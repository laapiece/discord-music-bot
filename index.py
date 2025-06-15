import os
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import yt_dlp
from dotenv import load_dotenv
import logging
import traceback
from flask import Flask, request, jsonify
from threading import Thread
import logging
import json
import sys

# --- Basic Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
log = logging.getLogger(__name__)

# --- Environment Variables ---
load_dotenv()
TOKEN = os.getenv('TOKEN')
API_TOKEN = os.getenv('API_TOKEN', 'SECRET_API_TOKEN')  # Secret pour l'API

if not TOKEN:
    log.error("ERROR: Discord TOKEN not found in .env file.")
    exit()

# --- yt-dlp Configuration ---
ytdl_format_options = {
    'format': 'bestaudio[ext=webm]/bestaudio/best',
    'outtmpl': 'downloads/%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'extract_flat': 'in_playlist',
    'skip_download': True,
    'concurrent_fragment_downloads': 2,  # Réduit la charge CPU/RAM
    'ratelimit': 500000,# Limite la bande passante (500KB/s)
    'socket_timeout': 15
}

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 20 -reconnect_on_network_error 1',  # Augmente le délai max à 20s
    'options': '-vn -filter:a "volume=0.25" -bufsize 4096k'  # Buffer augmenté pour stabilité
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

# --- Intents ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

# --- Bot Setup ---
bot = commands.Bot(
    command_prefix='!',
    intents=intents,
    gateway_params={"large_threshold": 100, "http": {"version": 2}}  # Améliore les performances réseau
)

# --- API Setup ---
app = Flask(__name__)

# Désactiver le logging de Flask
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# --- Global Player Dictionary ---
players = {}  # guild_id: MusicPlayer instance

# --- Audio Source Class ---
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title', 'Unknown Title')
        self.url = data.get('webpage_url', '#')
        self.thumbnail = data.get('thumbnail')
        self.duration = data.get('duration')
        self.uploader = data.get('uploader')
        self.requester = data.get('requester')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True, requester=None):
        loop = loop or asyncio.get_event_loop()
        
        try:
            data = await loop.run_in_executor(
                None,
                lambda: ytdl.extract_info(url, download=not stream)
            )
        except yt_dlp.utils.DownloadError as e:
            raise ValueError(f"Could not process link. YTDL Error: {e}")
        except Exception as e:
            raise ValueError(f"An unexpected error occurred: {e}")

        if not data:
            raise ValueError("Could not retrieve video data.")

        if 'entries' in data:
            data = data['entries'][0]
            if not data:
                raise ValueError("No valid entries found.")

        stream_url = data.get('url')
        if not stream_url:
            raise ValueError("Could not find a direct streamable URL.")

        data['requester'] = requester
        audio_source = discord.FFmpegPCMAudio(stream_url, **ffmpeg_options)
        return cls(audio_source, data=data)

    @classmethod
    async def search(cls, query: str, *, loop=None, requester=None):
        loop = loop or asyncio.get_event_loop()
        
        try:
            search_query = query if query.startswith(('https://', 'http://')) else f"ytsearch1:{query}"
            data = await loop.run_in_executor(
                None,
                lambda: ytdl.extract_info(search_query, download=False)
            )

            if not data:
                raise ValueError("Search returned no results.")

            if 'entries' in data and data['entries']:
                first_result_url = data['entries'][0].get('webpage_url')
                if not first_result_url:
                    data = data['entries'][0]
                else:
                    return await cls.from_url(first_result_url, loop=loop, stream=True, requester=requester)
            
            if 'webpage_url' in data:
                return await cls.from_url(data['webpage_url'], loop=loop, stream=True, requester=requester)
            
            raise ValueError("Could not find a playable video from the query.")

        except yt_dlp.utils.DownloadError as e:
            raise ValueError(f"Could not find or process '{query}'. YTDL Error: {e}")
        except Exception as e:
            raise ValueError(f"An unexpected error occurred during search: {e}")

# --- Music Player Class ---
class MusicPlayer:
    def __init__(self, interaction: discord.Interaction):
        self.bot = interaction.client
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.voice_client = interaction.guild.voice_client
        self.queue = asyncio.Queue()
        self.next = asyncio.Event()
        self.current_source = None
        self._loop_task = None
        self.volume = 0.5
        self.playing = False
        self.heartbeat = self.bot.loop.create_task(voice_heartbeat(self))

        self._loop_task = self.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        await self.bot.wait_until_ready()
        log.info(f"Player loop started for guild {self.guild.id}")

        while True:
            self.next.clear()

            try:
                async with asyncio.timeout(300):
                    source = await self.queue.get()
                    log.debug(f"[{self.guild.id}] Got song from queue: {source.title}")
            except asyncio.TimeoutError:
                log.info(f"[{self.guild.id}] Player inactive for 5 minutes. Disconnecting.")
                await self.destroy()
                return
            except asyncio.CancelledError:
                log.info(f"[{self.guild.id}] Player loop cancelled.")
                return

            if not self.voice_client or not self.voice_client.is_connected():
                log.warning(f"[{self.guild.id}] Voice client disconnected unexpectedly. Stopping loop.")
                await self.destroy()
                return

            self.current_source = source
            self.playing = True

            try:
                log.info(f"[{self.guild.id}] Playing: {source.title}")
                self.voice_client.play(source, after=self.handle_after_play)
                source.volume = self.volume
            except Exception as e:
                log.error(f"[{self.guild.id}] Error playing source {source.title}: {e}\n{traceback.format_exc()}")
                self.next.set()

            await self.next.wait()
            log.debug(f"[{self.guild.id}] Song finished or skipped: {source.title}")
            self.current_source = None
            self.playing = False

    def handle_after_play(self, error):
        if error:
            log.error(f"[{self.guild.id}] Error during playback: {error}")
        self.bot.loop.call_soon_threadsafe(self.next.set)

    async def add_to_queue(self, source: YTDLSource):
        await self.queue.put(source)
        log.info(f"[{self.guild.id}] Added to queue: {source.title} (Queue size: {self.queue.qsize()})")

    async def destroy(self):
        log.info(f"[{self.guild.id}] Destroying music player.")
        if self._loop_task:
            self._loop_task.cancel()
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if self.voice_client and self.voice_client.is_connected():
            self.voice_client.stop()
            await self.voice_client.disconnect()
        players.pop(self.guild.id, None)
        
    async def toggle_pause(self):
        if self.voice_client.is_playing():
            self.voice_client.pause()
            self.playing = False
            return False
        elif self.voice_client.is_paused():
            self.voice_client.resume()
            self.playing = True
            return True
        return self.playing

    async def skip_current(self):
        if self.voice_client.is_playing() or self.voice_client.is_paused():
            self.voice_client.stop()
            return True
        return False

    async def set_volume(self, volume):
        self.volume = volume / 100.0
        if self.voice_client and self.voice_client.source:
            self.voice_client.source.volume = self.volume
        return True

    def get_queue_info(self):
        queue_list = []
        for i in range(self.queue.qsize()):
            try:
                source = self.queue._queue[i]
                queue_list.append({
                    "title": source.title,
                    "url": source.url,
                    "requester": source.requester.name if source.requester else "Unknown"
                })
            except:
                pass
        return queue_list

# --- Helper Functions ---
def format_duration(seconds: int):
    if seconds is None: return "N/A"
    try:
        seconds = int(seconds)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        else:
            return f"{minutes}:{seconds:02d}"
    except (ValueError, TypeError):
        return "N/A"


async def get_player(interaction: discord.Interaction) -> MusicPlayer:
    guild_id = interaction.guild.id
    if guild_id in players:
        return players[guild_id]
    else:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("You need to be in a voice channel to start playing music.", ephemeral=True)
            return None

        voice_channel = interaction.user.voice.channel
        permissions = voice_channel.permissions_for(interaction.guild.me)
        if not permissions.connect or not permissions.speak:
            await interaction.response.send_message("I don't have permission to connect or speak in that channel.", ephemeral=True)
            return None

        try:
            voice_client = await voice_channel.connect()
        except discord.ClientException as e:
            await interaction.response.send_message(f"Failed to connect to voice channel: {e}", ephemeral=True)
            return None
        except Exception as e:
            log.error(f"Unexpected error connecting to VC: {e}")
            await interaction.response.send_message(f"An unexpected error occurred while connecting.", ephemeral=True)
            return None

        players[guild_id] = MusicPlayer(interaction)
        log.info(f"Created new MusicPlayer for guild {guild_id}")
        return players[guild_id]

async def voice_heartbeat(player):
    while True:
        await asyncio.sleep(10)
        if not player.voice_client.is_connected():
            await player.destroy()
            break
        elif not player.voice_client.is_playing():
            player.voice_client.stop()

# --- Bot Events ---
@bot.event
async def on_ready():
    log.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    log.info(f'Discord.py version: {discord.__version__}')
    
    try:
        synced = await bot.tree.sync()
        log.info(f'Synced {len(synced)} slash commands.')
    except Exception as e:
        log.error(f"Failed to sync slash commands: {e}")

    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="Tagilla 🤺"))

@bot.event
async def on_voice_state_update(member, before, after):
    if member.id == bot.user.id and not after.channel:
        log.info(f"Bot disconnected from voice channel in guild {member.guild.id}")
        player = players.get(member.guild.id)
        if player:
            log.info(f"Cleaning up player for guild {member.guild.id} due to disconnection.")
            await player.destroy()

    elif before.channel and not after.channel and member.guild.voice_client:
        if member.guild.voice_client.channel == before.channel:
            if len(before.channel.members) == 1 and before.channel.members[0].id == bot.user.id:
                log.info(f"Voice channel {before.channel.name} became empty in guild {member.guild.id}. Disconnecting.")
                player = players.get(member.guild.id)
                if player:
                    await player.destroy()

# --- Slash Commands ---
@bot.tree.command(name="play", description="Plays a song from YouTube, Spotify (via YT search), or URL.")
@app_commands.describe(query="The song title, YouTube URL, or Spotify URL to play.")
async def play(interaction: discord.Interaction, *, query: str):
    await interaction.response.defer()

    player = await get_player(interaction)
    if not player:
        return

    try:
        source = await YTDLSource.search(query, loop=bot.loop, requester=interaction.user)
        await player.add_to_queue(source)

        embed = discord.Embed(
            title="✅ Added to Queue",
            description=f"**[{source.title}]({source.url})**",
            color=discord.Color.green()
        )
        if source.thumbnail:
            embed.set_thumbnail(url=source.thumbnail)
        if source.duration:
            embed.add_field(name="Duration", value=format_duration(source.duration), inline=True)
        embed.set_footer(text=f"Position in queue: {player.queue.qsize()}")

        await interaction.followup.send(embed=embed)

    except ValueError as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
    except Exception as e:
        log.error(f"[{interaction.guild.id}] Unexpected error in /play command for query '{query}': {e}\n{traceback.format_exc()}")
        await interaction.followup.send(f"❌ An unexpected error occurred. Please try again later.", ephemeral=True)

@bot.tree.command(name="stop", description="Stops the music, clears the queue, and disconnects the bot.")
async def stop(interaction: discord.Interaction):
    player = players.get(interaction.guild.id)
    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_connected():
        return await interaction.response.send_message("I'm not currently in a voice channel.", ephemeral=True)

    if player:
        await player.destroy()
    elif voice_client:
        await voice_client.disconnect()

    await interaction.response.send_message("🛑 Playback stopped, queue cleared, and disconnected.")
    log.info(f"[{interaction.guild.id}] Stop command executed by {interaction.user.name}")

@bot.tree.command(name="skip", description="Skips the currently playing song.")
async def skip(interaction: discord.Interaction):
    player = players.get(interaction.guild.id)
    voice_client = interaction.guild.voice_client

    if not voice_client or not (voice_client.is_playing() or voice_client.is_paused()):
        return await interaction.response.send_message("I'm not playing anything right now.", ephemeral=True)

    if player and player.current_source:
        log.info(f"[{interaction.guild.id}] Skipping song: {player.current_source.title} by request of {interaction.user.name}")
        await player.skip_current()
        await interaction.response.send_message(f"⏭️ Skipped **{player.current_source.title}**.")
    else:
        voice_client.stop()
        await interaction.response.send_message("⏭️ Skipping current track.")

@bot.tree.command(name="volume", description="Adjusts the playback volume (0-200%).")
@app_commands.describe(level="The desired volume percentage (e.g., 50 for 50%).")
@app_commands.checks.bot_has_permissions(speak=True)
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 200]):
    player = players.get(interaction.guild.id)
    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_connected():
        return await interaction.response.send_message("I'm not connected to a voice channel.", ephemeral=True)

    if player:
        await player.set_volume(level)
        log.info(f"[{interaction.guild.id}] Volume set to {level}% by {interaction.user.name}")
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")
    else:
        await interaction.response.send_message("Couldn't adjust volume right now (no active player).", ephemeral=True)

@bot.tree.command(name="queue", description="Shows the current song queue.")
async def queue(interaction: discord.Interaction):
    player = players.get(interaction.guild.id)

    if not player or (player.queue.empty() and not player.current_source):
        return await interaction.response.send_message("The queue is currently empty.", ephemeral=True)

    embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.purple())

    if player.current_source:
        requester = player.current_source.requester
        embed.add_field(
            name="▶️ Now Playing",
            value=f"**[{player.current_source.title}]({player.current_source.url})** | `{format_duration(player.current_source.duration)}` | Requested by: {requester.mention if requester else 'Unknown'}",
            inline=False
        )
    else:
        embed.add_field(name="▶️ Now Playing", value="Nothing currently playing.", inline=False)

    if not player.queue.empty():
        queue_list = []
        for i in range(min(10, player.queue.qsize())):
            source = player.queue._queue[i]
            requester = source.requester
            queue_list.append(
                f"`{i+1}.` **[{source.title}]({source.url})** | `{format_duration(source.duration)}` | Req by: {requester.mention if requester else 'Unknown'}"
            )

        if queue_list:
            embed.add_field(name=f"⏭️ Up Next ({player.queue.qsize()} total)", value="\n".join(queue_list), inline=False)
        if player.queue.qsize() > 10:
            embed.set_footer(text=f"... and {player.queue.qsize() - 10} more.")
    else:
        embed.add_field(name="⏭️ Up Next", value="Queue is empty.", inline=False)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="pause", description="Pauses the current song.")
async def pause(interaction: discord.Interaction):
    player = players.get(interaction.guild.id)
    if not player or not player.voice_client:
        return await interaction.response.send_message("I'm not currently playing anything.", ephemeral=True)
    
    if player.voice_client.is_playing():
        await player.toggle_pause()
        await interaction.response.send_message("⏸️ Playback paused.")
    elif player.voice_client.is_paused():
        await interaction.response.send_message("Playback is already paused.", ephemeral=True)
    else:
        await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)

@bot.tree.command(name="resume", description="Resumes the paused song.")
async def resume(interaction: discord.Interaction):
    player = players.get(interaction.guild.id)
    if not player or not player.voice_client:
        return await interaction.response.send_message("I'm not currently playing anything.", ephemeral=True)
    
    if player.voice_client.is_paused():
        await player.toggle_pause()
        await interaction.response.send_message("▶️ Playback resumed.")
    elif player.voice_client.is_playing():
        await interaction.response.send_message("Playback is already playing.", ephemeral=True)
    else:
        await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)

@bot.tree.command(name="ping", description="Checks the bot's latency.")
async def ping(interaction: discord.Interaction):
    latency = bot.latency * 1000
    await interaction.response.send_message(f"Pong! Latency: {latency:.2f} ms")

# --- API Endpoints ---
@app.route('/guilds', methods=['GET'])
def get_guilds():
    """Renvoie la liste des serveurs où le bot est présent"""
    guilds = [{"id": str(guild.id), "name": guild.name} for guild in bot.guilds]
    return jsonify({"guilds": guilds})

@app.route('/guilds/<int:guild_id>/voice_channels', methods=['GET'])
def get_voice_channels(guild_id):
    """Renvoie la liste des salons vocaux d'un serveur"""
    guild = bot.get_guild(guild_id)
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    voice_channels = []
    for channel in guild.voice_channels:
        voice_channels.append({
            "id": str(channel.id),
            "name": channel.name
        })

    return jsonify({"voice_channels": voice_channels})

@app.route('/status', methods=['GET'])
def get_status():
    """Renvoie l'état actuel de la musique pour un serveur spécifique"""
    guild_id = request.args.get('guild_id')
    if not guild_id:
        return jsonify({"error": "Missing guild_id parameter"}), 400

    try:
        guild_id = int(guild_id)
    except ValueError:
        return jsonify({"error": "Invalid guild_id format"}), 400

    player = players.get(guild_id)
    if not player:
        return jsonify({"connected": False, "message": "Bot not connected in this server"})

    current_track = None
    if player.current_source:
        current_track = {
            "title": player.current_source.title,
            "url": player.current_source.url,
            "thumbnail": player.current_source.thumbnail,
            "duration": format_duration(player.current_source.duration),
            "requester": player.current_source.requester.name if player.current_source.requester else "Dashboard"
        }

    queue_info = player.get_queue_info()

    return jsonify({
        "connected": True,
        "playing": player.playing,
        "volume": int(player.volume * 100),
        "current": current_track,
        "queue": queue_info
    })

@app.route('/play', methods=['POST'])
def play_music():
    """Ajoute une musique à la file d'attente"""
    auth_token = request.headers.get('Authorization')
    if auth_token != f"Bearer {API_TOKEN}":
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    guild_id = data.get('guild_id')
    channel_id = data.get('channel_id')
    url = data.get('url')
    requester = data.get('requester', "Dashboard")

    if not guild_id or not channel_id or not url:
        return jsonify({"success": False, "message": "Missing parameters"}), 400

    try:
        guild = bot.get_guild(int(guild_id))
        if not guild:
            return jsonify({"success": False, "message": "Server not found"}), 404

        channel = guild.get_channel(int(channel_id))
        if not channel or not isinstance(channel, discord.VoiceChannel):
            return jsonify({"success": False, "message": "Voice channel not found"}), 404

        # Simuler une interaction pour obtenir le player
        class MockInteraction:
            def __init__(self, guild, channel):
                self.guild = guild
                self.channel = channel
                self.user = type('User', (object,), {"name": requester, "mention": requester})()
                self.response = type('Response', (object,), {})()
                
            async def defer(self):
                pass

        mock_interaction = MockInteraction(guild, channel)
        player = players.get(guild.id)

        async def add_music():
            nonlocal player
            if not player:
                # Connecter le bot au salon vocal
                voice_client = await channel.connect()
                
                # Créer un player
                player = MusicPlayer(mock_interaction)
                players[guild.id] = player
                
            # Ajouter la musique
            source = await YTDLSource.search(url, loop=bot.loop, requester=mock_interaction.user)
            await player.add_to_queue(source)
            return source

        future = asyncio.run_coroutine_threadsafe(add_music(), bot.loop)
        source = future.result()

        return jsonify({
            "success": True,
            "message": "Music added to queue",
            "title": source.title,
            "position": player.queue.qsize()
        })

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/pause', methods=['POST'])
def pause_music():
    """Met en pause ou relance la lecture"""
    auth_token = request.headers.get('Authorization')
    if auth_token != f"Bearer {API_TOKEN}":
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    guild_id = data.get('guild_id')
    
    if not guild_id:
        return jsonify({"success": False, "message": "Missing guild_id"}), 400

    try:
        guild_id = int(guild_id)
    except ValueError:
        return jsonify({"success": False, "message": "Invalid guild_id"}), 400

    player = players.get(guild_id)
    if not player:
        return jsonify({"success": False, "message": "Player not found"}), 404

    async def toggle():
        return await player.toggle_pause()

    future = asyncio.run_coroutine_threadsafe(toggle(), bot.loop)
    new_state = future.result()
    
    return jsonify({
        "success": True,
        "playing": new_state,
        "message": "Paused" if not new_state else "Resumed"
    })

@app.route('/skip', methods=['POST'])
def skip_music():
    """Passe à la musique suivante"""
    auth_token = request.headers.get('Authorization')
    if auth_token != f"Bearer {API_TOKEN}":
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    guild_id = data.get('guild_id')
    
    if not guild_id:
        return jsonify({"success": False, "message": "Missing guild_id"}), 400

    try:
        guild_id = int(guild_id)
    except ValueError:
        return jsonify({"success": False, "message": "Invalid guild_id"}), 400

    player = players.get(guild_id)
    if not player:
        return jsonify({"success": False, "message": "Player not found"}), 404

    async def skip():
        return await player.skip_current()

    future = asyncio.run_coroutine_threadsafe(skip(), bot.loop)
    success = future.result()
    
    if success:
        return jsonify({"success": True, "message": "Skipped current track"})
    else:
        return jsonify({"success": False, "message": "Nothing to skip"}), 400

@app.route('/volume', methods=['POST'])
def set_volume():
    """Règle le volume"""
    auth_token = request.headers.get('Authorization')
    if auth_token != f"Bearer {API_TOKEN}":
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    guild_id = data.get('guild_id')
    volume = data.get('volume')
    
    if not guild_id or volume is None:
        return jsonify({"success": False, "message": "Missing parameters"}), 400

    try:
        guild_id = int(guild_id)
    except ValueError:
        return jsonify({"success": False, "message": "Invalid guild_id"}), 400

    player = players.get(guild_id)
    if not player:
        return jsonify({"success": False, "message": "Player not found"}), 404

    async def set_vol():
        return await player.set_volume(volume)

    future = asyncio.run_coroutine_threadsafe(set_vol(), bot.loop)
    success = future.result()
    
    if success:
        return jsonify({"success": True, "message": f"Volume set to {volume}%"})
    else:
        return jsonify({"success": False, "message": "Failed to set volume"}), 500


# --- API Middleware ---
@app.before_request
def check_auth():
    # Exclure la route /guilds de l'authentification
    if request.path == '/guilds' or request.path.startswith('/guilds/'):
        return
        
    auth_token = request.headers.get('Authorization')
    if not auth_token or auth_token != f"Bearer {API_TOKEN}":
        return jsonify({"error": "Unauthorized"}), 401



# --- Run Bot and API ---
def run_flask():
    app.run(host='0.0.0.0', port=5000)

if __name__ == "__main__":
    # Correction pour Windows
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # Démarrer le serveur API dans un thread séparé
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Démarrer le bot Discord
    bot.run(TOKEN)
