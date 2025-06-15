import os
import discord
from discord.ext import commands
from discord import app_commands # Import app_commands
import asyncio
import platform
import yt_dlp
from dotenv import load_dotenv
import logging
import traceback

# --- Basic Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
log = logging.getLogger(__name__)

# --- Environment Variables ---
load_dotenv()
TOKEN = os.getenv('TOKEN')
if not TOKEN:
    log.error("ERROR: Discord TOKEN not found in .env file.")
    exit()

# --- yt-dlp Configuration ---
# REMOVED volume=2.0 - this was likely causing saturation.
# Volume control will be handled by PCMVolumeTransformer.
ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(extractor)s-%(id)s-%(title)s.%(ext)s', # Store downloads in a folder
    'restrictfilenames': True,
    'noplaylist': True, # Process only single videos unless explicitly handling playlists
    'nocheckcertificate': True,
    'ignoreerrors': False, # Let errors propagate so we can catch them
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',  # Bind to ipv4 if needed
    'extract_flat': 'in_playlist', # Faster playlist handling if needed later
    'skip_download': True # Prefer streaming
}

ffmpeg_options = {
    'options': '-vn',
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

# --- Intents ---
intents = discord.Intents.default()
intents.message_content = True # Still needed for potential future non-slash features or debugging
intents.voice_states = True

# --- Bot Setup (Using commands.Bot for easier transition, but enabling slash commands) ---
bot = commands.Bot(command_prefix='!', intents=intents) # Prefix command support can be removed if only slash is desired

# --- Audio Source Class ---
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5): # Default volume set here
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title', 'Unknown Title')
        self.url = data.get('webpage_url', '#') # Use webpage_url for the link
        self.thumbnail = data.get('thumbnail')
        self.duration = data.get('duration')
        self.uploader = data.get('uploader')
        self.requester = data.get('requester') # Store who requested the song

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True, requester=None):
        loop = loop or asyncio.get_event_loop()
        log.info(f"Attempting to extract info for: {url}")
        try:
            # Use run_in_executor for blocking I/O
            data = await loop.run_in_executor(
                None,
                lambda: ytdl.extract_info(url, download=not stream) # Use stream=True by default
            )
        except yt_dlp.utils.DownloadError as e:
            log.error(f"YTDL DownloadError for {url}: {e}")
            # Handle specific errors if needed (e.g., age restriction, private video)
            if "is age restricted" in str(e):
                 raise ValueError("Video is age restricted and cannot be played.")
            elif "Private video" in str(e):
                 raise ValueError("This is a private video.")
            elif "Video unavailable" in str(e):
                 raise ValueError("Video is unavailable.")
            else:
                 raise ValueError(f"Could not process link. YTDL Error: {e}") # Generic error
        except Exception as e:
            log.error(f"Generic Exception during YTDL extraction for {url}: {e}")
            raise ValueError(f"An unexpected error occurred with yt-dlp: {e}")


        if not data:
             raise ValueError("Could not retrieve video data.")

        # Handle playlists or searches returning multiple entries
        if 'entries' in data:
            # Take the first item from a playlist or search result
            log.info(f"Found playlist/search results, taking first item for '{url}'")
            data = data['entries'][0]
            if not data:
                raise ValueError("Playlist/Search found, but no valid entries.")

        # Get the streaming URL (might be different from the initial URL)
        stream_url = data.get('url')
        if not stream_url:
             log.warning(f"No direct stream URL found for {data.get('title')}. Trying download=True.")
             # Retry with download enabled if streaming url not found (might happen for some sites)
             # Be cautious with this, it will download files
             # For simplicity now, we'll just raise an error if no direct URL
             raise ValueError("Could not find a direct streamable URL for this video.")


        log.info(f"Successfully extracted: {data.get('title')}")
        filename = stream_url # Use the direct stream URL obtained from extract_info

        # Store the requester in the data dict
        data['requester'] = requester

        # Create the audio source
        try:
             audio_source = discord.FFmpegPCMAudio(filename, **ffmpeg_options)
        except Exception as e:
             log.error(f"Failed to create FFmpegPCMAudio source for {filename}: {e}")
             raise ValueError(f"Failed to initialize audio player (FFmpeg error): {e}")

        return cls(audio_source, data=data)

    @classmethod
    async def search(cls, query: str, *, loop=None, requester=None):
        """Search for a video/song and return the source."""
        loop = loop or asyncio.get_event_loop()
        log.info(f"Searching for: {query}")
        try:
            # Use ytsearch for explicit search, let yt-dlp handle URLs otherwise
            search_query = query if query.startswith(('https://', 'http://')) else f"ytsearch1:{query}"

            data = await loop.run_in_executor(
                None,
                lambda: ytdl.extract_info(search_query, download=False) # Don't download for search
            )

            if not data:
                raise ValueError("Search returned no results.")

            # If search (ytsearch), 'entries' will contain the results
            if 'entries' in data and data['entries']:
                # Get the URL of the first search result
                first_result_url = data['entries'][0].get('webpage_url')
                if not first_result_url:
                     # Sometimes ytsearch provides needed info directly
                     log.info("ytsearch provided direct info, using that.")
                     data = data['entries'][0] # Use data from first entry directly
                else:
                     log.info(f"Search successful for '{query}', found URL: {first_result_url}")
                     # Now get the full info for the specific URL for streaming
                     return await cls.from_url(first_result_url, loop=loop, stream=True, requester=requester)
            # If it was a direct URL, data might be the video info itself
            elif 'webpage_url' in data:
                 log.info(f"Direct URL provided: {data.get('webpage_url')}")
                 return await cls.from_url(data['webpage_url'], loop=loop, stream=True, requester=requester)
            else:
                 raise ValueError("Could not find a playable video from the query.")

            # If we fell through here, it means ytsearch gave direct info
            stream_url = data.get('url')
            if not stream_url:
                raise ValueError("Search result did not contain a streamable URL.")

            log.info(f"Successfully extracted from search result: {data.get('title')}")
            data['requester'] = requester # Add requester info
            audio_source = discord.FFmpegPCMAudio(stream_url, **ffmpeg_options)
            return cls(audio_source, data=data)


        except yt_dlp.utils.DownloadError as e:
            log.error(f"YTDL DownloadError during search for '{query}': {e}")
            raise ValueError(f"Could not find or process '{query}'. YTDL Error: {e}")
        except Exception as e:
            log.error(f"Generic Exception during search for '{query}': {e}\n{traceback.format_exc()}")
            raise ValueError(f"An unexpected error occurred during search: {e}")


# --- Music Player Class ---
class MusicPlayer:
    def __init__(self, interaction: discord.Interaction):
        self.bot = interaction.client
        self.guild = interaction.guild
        self.channel = interaction.channel # Store the channel for messages
        self.voice_client = interaction.guild.voice_client
        self.queue = asyncio.Queue() # Use asyncio.Queue for better async handling
        self.next = asyncio.Event()
        self.current_source = None # Holds the YTDLSource object currently playing
        self._loop_task = None # To hold the player loop task

        # Start the player loop
        self._loop_task = self.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        """Main loop for playing songs from the queue."""
        await self.bot.wait_until_ready()
        log.info(f"Player loop started for guild {self.guild.id}")

        while True:
            self.next.clear()

            # Get the next song source from the queue
            try:
                # Wait indefinitely until a song is available or the task is cancelled
                log.debug(f"[{self.guild.id}] Waiting for next song in queue...")
                async with asyncio.timeout(300): # 5 minutes inactivity timeout
                     source = await self.queue.get()
                     log.debug(f"[{self.guild.id}] Got song from queue: {source.title}")

            except asyncio.TimeoutError:
                 log.info(f"[{self.guild.id}] Player inactive for 5 minutes. Disconnecting.")
                 await self.destroy()
                 return # Exit the loop
            except asyncio.CancelledError:
                 log.info(f"[{self.guild.id}] Player loop cancelled.")
                 return # Exit the loop


            # Check if voice client is still valid
            if not self.voice_client or not self.voice_client.is_connected():
                log.warning(f"[{self.guild.id}] Voice client disconnected unexpectedly. Stopping loop.")
                await self.destroy() # Clean up
                return

            self.current_source = source

            # --- Play the song ---
            try:
                log.info(f"[{self.guild.id}] Playing: {source.title}")
                self.voice_client.play(source, after=self.handle_after_play)
            except Exception as e:
                log.error(f"[{self.guild.id}] Error playing source {source.title}: {e}\n{traceback.format_exc()}")
                # Try to send error message to the channel where the player was initiated
                try:
                     await self.channel.send(f"❌ Error playing **{source.title}**: {e}. Skipping.")
                except discord.HTTPException:
                     log.error(f"[{self.guild.id}] Could not send error message to channel {self.channel.id}")
                # Signal the loop to continue immediately to the next song
                self.next.set() # Important: ensure loop continues even if play fails


            # --- Send "Now Playing" message ---
            embed = discord.Embed(
                title="🎵 Now Playing",
                description=f"**[{source.title}]({source.url})**",
                color=discord.Color.blue()
            )
            if source.thumbnail:
                embed.set_thumbnail(url=source.thumbnail)
            if source.duration:
                embed.add_field(name="Duration", value=format_duration(source.duration), inline=True)
            if source.uploader:
                 embed.add_field(name="Uploader", value=source.uploader, inline=True)
            if source.requester:
                embed.add_field(name="Requested by", value=source.requester.mention, inline=False)

            try:
                await self.channel.send(embed=embed)
            except discord.HTTPException:
                log.warning(f"[{self.guild.id}] Could not send 'Now Playing' message.")


            # Wait until the 'after' callback signals the song is done (or skipped/stopped)
            await self.next.wait()
            log.debug(f"[{self.guild.id}] Song finished or skipped: {source.title}")

            # Reset current source after finishing
            self.current_source = None
            # queue.task_done() # Mark task as done if using queue processing logic elsewhere


    def handle_after_play(self, error):
        """Callback function run after a song finishes or errors during playback."""
        if error:
            log.error(f"[{self.guild.id}] Error during playback: {error}")
            # Optionally send a message to the channel about the error
            # coro = self.channel.send(f"Playback error: {error}")
            # self.bot.loop.create_task(coro)

        # Regardless of error, signal the player_loop to continue
        log.debug(f"[{self.guild.id}] 'after' callback triggered.")
        self.bot.loop.call_soon_threadsafe(self.next.set)


    async def add_to_queue(self, source: YTDLSource):
        """Adds a song source to the queue."""
        await self.queue.put(source)
        log.info(f"[{self.guild.id}] Added to queue: {source.title} (Queue size: {self.queue.qsize()})")

    async def destroy(self):
        """Cleans up the player: stops playback, clears queue, disconnects."""
        log.info(f"[{self.guild.id}] Destroying music player.")
        # Cancel the player loop task
        if self._loop_task:
            self._loop_task.cancel()
        # Clear the queue
        while not self.queue.empty():
             try:
                 self.queue.get_nowait()
             except asyncio.QueueEmpty:
                 break
        # Stop playback and disconnect
        if self.voice_client and self.voice_client.is_connected():
            self.voice_client.stop()
            await self.voice_client.disconnect()
        # Remove from global players dictionary
        players.pop(self.guild.id, None)

# --- Global Player Dictionary ---
players = {} # guild_id: MusicPlayer instance

# --- Helper Functions ---
def format_duration(seconds: int):
    """Formats seconds into MM:SS or HH:MM:SS"""
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
    """Gets or creates a MusicPlayer for the guild."""
    guild_id = interaction.guild.id
    if guild_id in players:
        # Ensure voice client is still valid, reconnect if necessary?
        # For simplicity, we assume it's okay or rely on user to reinvoke if needed.
        return players[guild_id]
    else:
        # Check if user is in a voice channel
        if not interaction.user.voice or not interaction.user.voice.channel:
             await interaction.response.send_message("You need to be in a voice channel to start playing music.", ephemeral=True)
             return None

        # Check if bot has permissions to connect and speak
        voice_channel = interaction.user.voice.channel
        permissions = voice_channel.permissions_for(interaction.guild.me)
        if not permissions.connect or not permissions.speak:
             await interaction.response.send_message("I don't have permission to connect or speak in that channel.", ephemeral=True)
             return None

        # Connect to the voice channel
        try:
            log.info(f"Connecting to voice channel: {voice_channel.name} in guild {interaction.guild.name}")
            voice_client = await voice_channel.connect()
        except discord.ClientException as e:
             await interaction.response.send_message(f"Failed to connect to voice channel: {e}", ephemeral=True)
             return None
        except Exception as e:
             log.error(f"Unexpected error connecting to VC: {e}")
             await interaction.response.send_message(f"An unexpected error occurred while connecting.", ephemeral=True)
             return None


        # Create and store the player instance
        players[guild_id] = MusicPlayer(interaction) # Pass interaction to init
        log.info(f"Created new MusicPlayer for guild {guild_id}")
        return players[guild_id]

# --- Bot Events ---
@bot.event
async def on_ready():
    log.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    log.info(f'Discord.py version: {discord.__version__}')
    log.info('Syncing slash commands...')
    try:
        # Sync commands globally. Can take up to an hour to propagate.
        # For testing, sync to a specific guild:
        # await bot.tree.sync(guild=discord.Object(id=YOUR_TEST_GUILD_ID))
        synced = await bot.tree.sync()
        log.info(f'Synced {len(synced)} slash commands.')
    except Exception as e:
        log.error(f"Failed to sync slash commands: {e}")

    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="PTR 🤺"))

@bot.event
async def on_voice_state_update(member, before, after):
    """Handle bot disconnection or channel empty."""
    # If the bot itself was disconnected
    if member.id == bot.user.id and not after.channel:
        log.info(f"Bot disconnected from voice channel in guild {member.guild.id}")
        player = players.get(member.guild.id)
        if player:
            log.info(f"Cleaning up player for guild {member.guild.id} due to disconnection.")
            await player.destroy() # Ensure cleanup

    # If the bot is in a channel and it becomes empty (except for the bot)
    elif before.channel and not after.channel and member.guild.voice_client:
        if member.guild.voice_client.channel == before.channel:
             # Check if only the bot is left
             if len(before.channel.members) == 1 and before.channel.members[0].id == bot.user.id:
                  log.info(f"Voice channel {before.channel.name} became empty in guild {member.guild.id}. Disconnecting.")
                  player = players.get(member.guild.id)
                  if player:
                       await player.destroy()


# --- Slash Commands ---

@bot.tree.command(name="play", description="Plays a song from YouTube, Spotify (via YT search), or URL.")
@app_commands.describe(query="The song title, YouTube URL, or Spotify URL to play.")
async def play(interaction: discord.Interaction, *, query: str):
    """Plays a song or adds it to the queue."""
    await interaction.response.defer() # Acknowledge interaction, processing takes time

    player = await get_player(interaction)
    if not player:
        # get_player already sent the error message
        return

    try:
        log.info(f"[{interaction.guild.id}] Processing query: {query}")
        # Use the search method which handles both URLs and search terms
        source = await YTDLSource.search(query, loop=bot.loop, requester=interaction.user)

        await player.add_to_queue(source)

        # Provide feedback
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
        # User-friendly errors from YTDLSource or search
        log.warning(f"[{interaction.guild.id}] Value error during play command: {e}")
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
    except Exception as e:
        # Catch-all for unexpected errors
        log.error(f"[{interaction.guild.id}] Unexpected error in /play command for query '{query}': {e}\n{traceback.format_exc()}")
        await interaction.followup.send(f"❌ An unexpected error occurred. Please try again later.", ephemeral=True)


@bot.tree.command(name="stop", description="Stops the music, clears the queue, and disconnects the bot.")
async def stop(interaction: discord.Interaction):
    """Stops playback and disconnects."""
    player = players.get(interaction.guild.id)
    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_connected():
        return await interaction.response.send_message("I'm not currently in a voice channel.", ephemeral=True)

    if player:
        await player.destroy() # Use the destroy method for cleanup
    elif voice_client: # If player object somehow doesn't exist but bot is connected
         await voice_client.disconnect()

    await interaction.response.send_message("🛑 Playback stopped, queue cleared, and disconnected.")
    log.info(f"[{interaction.guild.id}] Stop command executed by {interaction.user.name}")


@bot.tree.command(name="skip", description="Skips the currently playing song.")
async def skip(interaction: discord.Interaction):
    """Skips the current song."""
    player = players.get(interaction.guild.id)
    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_playing():
        return await interaction.response.send_message("I'm not playing anything right now.", ephemeral=True)

    if player and player.current_source:
        log.info(f"[{interaction.guild.id}] Skipping song: {player.current_source.title} by request of {interaction.user.name}")
        voice_client.stop() # This triggers the 'after' callback, advancing the loop
        await interaction.response.send_message(f"⏭️ Skipped **{player.current_source.title}**.")
    else:
        # Fallback if state is inconsistent
        voice_client.stop()
        await interaction.response.send_message("⏭️ Skipping current track.")


@bot.tree.command(name="volume", description="Adjusts the playback volume (0-200%).")
@app_commands.describe(level="The desired volume percentage (e.g., 50 for 50%).")
# Use Range to automatically validate input
@app_commands.checks.bot_has_permissions(speak=True)
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 200]):
    """Sets the volume."""
    player = players.get(interaction.guild.id)
    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_connected():
        return await interaction.response.send_message("I'm not connected to a voice channel.", ephemeral=True)

    if not player or not player.current_source:
         # Allow setting volume even if not playing, it might affect the next song?
         # Or restrict to only when playing:
         # return await interaction.response.send_message("I'm not playing anything right now.", ephemeral=True)
         # For now, let's try setting it on the voice_client.source if available, might fail gracefully.
         pass # Continue and try setting


    # Adjust volume on the current source if possible
    if voice_client.source and isinstance(voice_client.source, discord.PCMVolumeTransformer):
         # Volume is float 0.0 to 2.0
         new_volume = level / 100.0
         voice_client.source.volume = new_volume
         log.info(f"[{interaction.guild.id}] Volume set to {level}% by {interaction.user.name}")
         await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")
    else:
         # This case might happen if called between songs or if source isn't a VolumeTransformer
         await interaction.response.send_message("Couldn't adjust volume right now (no active playback source?). Try again shortly.", ephemeral=True)


@bot.tree.command(name="queue", description="Shows the current song queue.")
async def queue(interaction: discord.Interaction):
    """Displays the queue."""
    player = players.get(interaction.guild.id)

    if not player or (player.queue.empty() and not player.current_source):
        return await interaction.response.send_message("The queue is currently empty.", ephemeral=True)

    embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.purple())

    # Current Song
    if player.current_source:
        requester = player.current_source.requester
        embed.add_field(
            name="▶️ Now Playing",
            value=f"**[{player.current_source.title}]({player.current_source.url})** | `{format_duration(player.current_source.duration)}` | Requested by: {requester.mention if requester else 'Unknown'}",
            inline=False
        )
    else:
        embed.add_field(name="▶️ Now Playing", value="Nothing currently playing.", inline=False)


    # Upcoming Songs
    if not player.queue.empty():
        queue_list = []
        # Need to peek into the asyncio.Queue without removing items
        items_in_queue = list(player.queue._queue) # Access internal deque (use with caution)
        for i, source in enumerate(items_in_queue[:10]): # Show next 10
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

@bot.tree.command(name="ping", description="Checks the bot's latency.")
async def ping(interaction: discord.Interaction):
    latency = bot.latency * 1000 # latency is in seconds
    await interaction.response.send_message(f"Pong! Latency: {latency:.2f} ms")


# --- Error Handling for Slash Commands ---
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.error(f"AppCommandError in guild {interaction.guild_id} for command {interaction.command.name if interaction.command else 'unknown'}: {error}\n{traceback.format_exc()}")

    if isinstance(error, app_commands.CommandNotFound):
        await interaction.response.send_message("Sorry, I don't recognize that command.", ephemeral=True)
    elif isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(f"You don't have permission to use this command: {error.missing_permissions}", ephemeral=True)
    elif isinstance(error, app_commands.BotMissingPermissions):
        await interaction.response.send_message(f"I don't have the required permissions: {error.missing_permissions}", ephemeral=True)
    elif isinstance(error, app_commands.CheckFailure):
         await interaction.response.send_message("You cannot run this command here or under these conditions.", ephemeral=True)
    elif isinstance(error, app_commands.CommandOnCooldown):
         await interaction.response.send_message(f"This command is on cooldown. Try again in {error.retry_after:.2f} seconds.", ephemeral=True)
    else:
        # Generic error fallback
        if interaction.response.is_done():
            await interaction.followup.send("An unexpected error occurred while processing your command.", ephemeral=True)
        else:
            await interaction.response.send_message("An unexpected error occurred while processing your command.", ephemeral=True)

# --- Run the Bot ---
if __name__ == "__main__":
    if not TOKEN:
        print("FATAL ERROR: DISCORD_TOKEN environment variable not set.")
    else:
        # Crée le dossier de téléchargements s'il n'existe pas
        if not os.path.exists('downloads'):
            os.makedirs('downloads')

        # --- AJOUT IMPORTANT POUR WINDOWS ---
        # Cette condition résout l'erreur "aiodns needs a SelectorEventLoop"
        if platform.system() == "Windows":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        # ------------------------------------

        try:
            log.info("Starting bot...")
            # Lancez le bot
            bot.run(TOKEN, log_handler=None)
        except discord.LoginFailure:
            log.error("Login failed: Invalid Discord Token. Please check your .env file.")
        except Exception as e:
            log.critical(f"Critical error running bot: {e}\n{traceback.format_exc()}")
