import asyncio
import os
import time
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Set, Tuple, Optional, Any
from functools import wraps
from telethon.sync import TelegramClient
from telethon.tl.functions.messages import DeleteHistoryRequest
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.types import User, InputUser, Channel, Chat
from telethon.errors import FloodWaitError, PeerIdInvalidError, UserIdInvalidError
from telethon.tl.custom.dialog import Dialog
from telethon import utils

# Configure logging
telethon_logger = logging.getLogger('telethon')
telethon_logger.setLevel(logging.ERROR)  # Only show errors, not INFO messages

# Then your existing logger setup continues
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("telegram_cleaner.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("TelegramCleaner")


# Emojis for output
EMOJI_SUCCESS = "✅"
EMOJI_ERROR = "❌"
EMOJI_WARNING = "⚠️"
EMOJI_INFO = "ℹ️"
EMOJI_MENU = "📋"
EMOJI_CLEANUP = "🧹"

# ANSI escape codes for colors
COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_YELLOW = "\033[93m"
COLOR_BLUE = "\033[94m"
COLOR_RESET = "\033[0m"

# Session name for TelegramClient
SESSION_NAME = "telegram_cleaner"


class RateLimiter:
    """A rate limiter for Telegram API requests that implements exponential backoff."""

    def __init__(self, initial_delay: float = 1.0, max_delay: float = 64.0, max_requests_per_minute: int = 20):
        self.delay = initial_delay
        self.max_delay = max_delay
        self.max_requests = max_requests_per_minute
        self.request_times = []
        self.last_error_time = None

    async def wait_if_needed(self):
        """Wait if we've hit the rate limit recently or made too many requests."""
        now = time.time()

        # Clean up old request times
        self.request_times = [t for t in self.request_times if t > now - 60]

        # Check if we've made too many requests in the last minute
        if len(self.request_times) >= self.max_requests:
            wait_time = 60 - (now - self.request_times[0])
            if wait_time > 0:
                logger.warning(f"{EMOJI_WARNING} Rate limit approaching. Waiting {wait_time:.2f}s...")
                await asyncio.sleep(wait_time)

        # Implement exponential backoff if we've had a recent error
        if self.last_error_time and now - self.last_error_time < self.delay * 10:
            logger.info(f"{EMOJI_INFO} Backing off for {self.delay:.2f}s due to recent error")
            await asyncio.sleep(self.delay)

        # Add current time to request times
        self.request_times.append(time.time())

    def record_success(self):
        """Record a successful API call and reduce the delay."""
        if self.delay > 1.0:
            self.delay = max(1.0, self.delay / 2)

    def record_error(self, wait_seconds: Optional[float] = None):
        """Record an error and increase the backoff delay."""
        self.last_error_time = time.time()
        if wait_seconds:
            self.delay = min(self.max_delay, wait_seconds * 1.5)
        else:
            self.delay = min(self.max_delay, self.delay * 2)


class EntityCache:
    """Cache for Telegram entities to reduce API calls."""

    def __init__(self, max_age: int = 3600):
        self.cache: Dict[str, Tuple[Any, float]] = {}
        self.max_age = max_age

    def get(self, key: str) -> Optional[Any]:
        """Get an entity from the cache if it exists and is not expired."""
        if key in self.cache:
            entity, timestamp = self.cache[key]
            if time.time() - timestamp < self.max_age:
                return entity
            else:
                # Remove expired entry
                del self.cache[key]
        return None

    def set(self, key: str, entity: Any):
        """Add an entity to the cache."""
        self.cache[key] = (entity, time.time())

    def invalidate(self, key: str = None):
        """Invalidate a specific entity or the entire cache."""
        if key:
            if key in self.cache:
                del self.cache[key]
        else:
            self.cache.clear()


class ProgressTracker:
    """Track progress of long-running operations."""

    def __init__(self, total: int, description: str = "Processing"):
        self.total = total
        self.current = 0
        self.description = description
        self.start_time = time.time()
        self.last_update = 0
        self.error_count = 0
        self.success_count = 0
        self._print_progress()

    def update(self, success: bool = True):
        """Update the progress by incrementing the counter."""
        self.current += 1
        if success:
            self.success_count += 1
        else:
            self.error_count += 1

        # Update the display at most once per second
        if time.time() - self.last_update > 1 or self.current == self.total:
            self._print_progress()
            self.last_update = time.time()

    def _print_progress(self):
        """Print the current progress."""
        if self.total == 0:
            percentage = 100
        else:
            percentage = (self.current / self.total) * 100

        # Calculate time remaining
        elapsed = time.time() - self.start_time
        if self.current > 0:
            items_per_second = self.current / elapsed
            remaining_items = self.total - self.current
            eta = remaining_items / items_per_second if items_per_second > 0 else 0
            eta_str = f"{eta:.1f}s remaining"
        else:
            eta_str = "calculating..."

        bar_length = 30
        filled_length = int(bar_length * self.current // self.total) if self.total > 0 else bar_length
        bar = '█' * filled_length + '-' * (bar_length - filled_length)

        print(f"\r{self.description}: [{bar}] {percentage:.1f}% ({self.current}/{self.total}) "
              f"✅{self.success_count} ❌{self.error_count} ETA: {eta_str}", end="")

        if self.current == self.total:
            print()  # Add newline at the end

    def complete(self):
        """Mark the operation as complete."""
        self.current = self.total
        self._print_progress()
        print()  # Add an extra newline for readability


class DataStorage:
    """Handles persistent storage of data."""

    def __init__(self, data_dir: str = "data"):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

    def _get_path(self, filename: str) -> str:
        """Get the full path for a file."""
        return os.path.join(self.data_dir, filename)

    def load_set(self, filename: str) -> Set[str]:
        """Load a set of items from a file."""
        path = self._get_path(filename)
        if os.path.exists(path):
            with open(path, "r", encoding='utf-8') as f:
                return set(line.strip() for line in f if line.strip())
        return set()

    def save_set(self, filename: str, data_set: Set[str]):
        """Save a set of items to a file."""
        path = self._get_path(filename)
        with open(path, "w", encoding='utf-8') as f:
            for item in sorted(data_set):
                f.write(f"{item}\n")

    def load_dict(self, filename: str) -> Dict:
        """Load a dictionary from a JSON file."""
        path = self._get_path(filename)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding='utf-8') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                logger.error(f"{EMOJI_ERROR} Error decoding JSON from {filename}")
                return {}
        return {}

    def save_dict(self, filename: str, data_dict: Dict):
        """Save a dictionary to a JSON file."""
        path = self._get_path(filename)
        with open(path, "w", encoding='utf-8') as f:
            json.dump(data_dict, f, indent=2)

    def load_deleted_accounts(self) -> List[InputUser]:
        """Load user IDs and access hashes from file."""
        path = self._get_path("deleted_accounts.txt")
        users = []
        try:
            with open(path, "r", encoding='utf-8') as f:
                for line in [line.strip() for line in f if line.strip()]:
                    try:
                        uid, access_hash = line.split(",")[:2]
                        users.append(InputUser(int(uid), int(access_hash)))
                    except ValueError:
                        logger.error(f"{EMOJI_ERROR} Invalid line in deleted_accounts.txt: {line}")
        except FileNotFoundError:
            logger.warning(f"{EMOJI_WARNING} deleted_accounts.txt not found")
        return users

    def save_credentials(self, api_id: str, api_hash: str):
        """Save API credentials to file."""
        path = self._get_path("credentials.txt")
        with open(path, "w") as f:
            f.write(f"{api_id},{api_hash}")

    def load_credentials(self) -> Tuple[Optional[str], Optional[str]]:
        """Load API credentials from file."""
        path = self._get_path("credentials.txt")
        if os.path.exists(path):
            with open(path, "r") as f:
                creds = f.read().strip().split(",")
                if len(creds) == 2:
                    return creds[0], creds[1]
        return None, None


class TelegramCleaner:
    """Main class for Telegram cleaning operations."""

    def __init__(self, concurrency_limit: int = 10):
        self.storage = DataStorage()
        self.entity_cache = EntityCache()
        self.rate_limiter = RateLimiter()
        self.concurrency_limit = concurrency_limit
        self.api_id = None
        self.api_hash = None
        self.client = None
        self.semaphore = None

    async def initialize(self):
        """Initialize the Telegram client and semaphore."""
        # Load or prompt for API credentials
        self.api_id, self.api_hash = self.storage.load_credentials()
        if not self.api_id or not self.api_hash:
            print(f"{COLOR_BLUE}{EMOJI_INFO} Please enter your Telegram API credentials.{COLOR_RESET}")
            self.api_id = input("API ID: ")
            self.api_hash = input("API Hash: ")
            self.storage.save_credentials(self.api_id, self.api_hash)
            print(f"{COLOR_GREEN}{EMOJI_SUCCESS} Saved API credentials.{COLOR_RESET}")

        # Create semaphore for concurrency control
        self.semaphore = asyncio.Semaphore(self.concurrency_limit)

    async def get_client(self):
        """Get or create a Telegram client."""
        if not self.client:
            self.client = TelegramClient(SESSION_NAME, self.api_id, self.api_hash)
            await self.client.start()
        return self.client

    async def close_client(self):
        """Close the Telegram client if it exists."""
        if self.client:
            await self.client.disconnect()
            self.client = None

    async def rate_limited_request(self, func, *args, **kwargs):
        """Execute a function with rate limiting."""
        await self.rate_limiter.wait_if_needed()
        try:
            result = await func(*args, **kwargs)
            self.rate_limiter.record_success()
            return result
        except FloodWaitError as e:
            self.rate_limiter.record_error(e.seconds)
            logger.warning(f"{EMOJI_WARNING} Rate limit hit, waiting {e.seconds}s...")
            await asyncio.sleep(e.seconds + 1)
            # Retry the request
            return await self.rate_limited_request(func, *args, **kwargs)
        except Exception as e:
            self.rate_limiter.record_error()
            raise e

    async def get_entity(self, entity_id):
        """Get an entity with caching."""
        cache_key = str(entity_id)
        entity = self.entity_cache.get(cache_key)
        if entity:
            return entity

        client = await self.get_client()
        try:
            entity = await self.rate_limited_request(client.get_entity, entity_id)
            self.entity_cache.set(cache_key, entity)
            return entity
        except Exception as e:
            logger.error(f"{EMOJI_ERROR} Error getting entity {entity_id}: {e}")
            raise

    async def scan_dead_bots(self):
        """Scan for dead bots with improved concurrency and progress tracking."""
        seen_bots = self.storage.load_set("seen_bots.txt")
        dead_bots = self.storage.load_set("dead_bots.txt")

        client = await self.get_client()

        logger.info(f"{EMOJI_INFO} Fetching your Telegram dialogs...")
        dialogs = await self.rate_limited_request(client.get_dialogs, limit=None)
        bot_users = [d.entity for d in dialogs if isinstance(d.entity, User) and d.entity.bot]
        new_bots = [b for b in bot_users if (b.username or str(b.id)) not in seen_bots]

        logger.info(f"{EMOJI_INFO} Found {len(bot_users)} bots, scanning {len(new_bots)} new ones...")

        if not new_bots:
            logger.info(f"{EMOJI_INFO} No new bots to scan.")
            return dead_bots

        progress = ProgressTracker(len(new_bots), "Scanning bots")

        async def ping_bot(bot):
            """Ping a single bot and check for response."""
            async with self.semaphore:
                username = bot.username or str(bot.id)
                try:
                    logger.debug(f"Pinging @{username}")

                    # Check if we've cached this bot as dead or alive
                    cache_key = f"bot_status_{username}"
                    cached_status = self.entity_cache.get(cache_key)
                    if cached_status:
                        logger.debug(f"Using cached status for @{username}: {'alive' if cached_status else 'dead'}")
                        progress.update(cached_status)
                        return username, cached_status

                    # Send a message and wait for response
                    await self.rate_limited_request(client.send_message, bot, '/start')
                    await asyncio.sleep(2)  # Wait for response

                    messages = await self.rate_limited_request(client.get_messages, bot, limit=3)

                    # Check if the bot responded with something other than /start
                    is_alive = bool(messages) and not all("/start" in m.message for m in messages)

                    # Cache the result
                    self.entity_cache.set(cache_key, is_alive)

                    if not is_alive:
                        logger.info(f"{EMOJI_ERROR} No response from @{username}")
                    else:
                        logger.info(f"{EMOJI_SUCCESS} @{username} responded")

                    progress.update(is_alive)
                    return username, is_alive
                except Exception as e:
                    logger.error(f"{EMOJI_ERROR} Error with @{username}: {e}")
                    progress.update(False)
                    return username, False

        # Create tasks with improved concurrency
        tasks = [ping_bot(bot) for bot in new_bots]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"{EMOJI_ERROR} Task exception: {result}")
                continue

            username, responded = result
            seen_bots.add(username)
            if not responded:
                dead_bots.add(username)

        # Save results
        self.storage.save_set("seen_bots.txt", seen_bots)
        self.storage.save_set("dead_bots.txt", dead_bots)

        progress.complete()
        logger.info(f"{EMOJI_INFO} Scan complete! Total dead bots: {len(dead_bots)}")

        return dead_bots

    async def scan_deleted_accounts(self):
        """Scan for deleted accounts with improved error handling."""
        client = await self.get_client()

        logger.info(f"{EMOJI_INFO} Scanning your Telegram dialogs...")
        dialogs = await self.rate_limited_request(client.get_dialogs, limit=None)

        contacts = [d.entity for d in dialogs if isinstance(d.entity, User) and not d.entity.bot]

        progress = ProgressTracker(len(contacts), "Checking accounts")
        deleted_users = []

        for user in contacts:
            if user.deleted:
                deleted_users.append(user)
            progress.update(not user.deleted)

        progress.complete()

        # Save results
        with open(self.storage._get_path("deleted_accounts.txt"), "w") as f:
            for user in deleted_users:
                f.write(f"{user.id},{user.access_hash}\n")

        logger.info(f"{EMOJI_SUCCESS} Saved {len(deleted_users)} deleted accounts")
        return deleted_users

    async def process_entity(self, entity_id, action):
        """Process a single entity with error handling."""
        async with self.semaphore:
            try:
                entity = await self.get_entity(entity_id)
                result = await self.rate_limited_request(action, entity)
                return True, entity_id
            except PeerIdInvalidError:
                logger.warning(f"{EMOJI_WARNING} Could not find entity {entity_id}")
                return False, entity_id
            except Exception as e:
                logger.error(f"{EMOJI_ERROR} Error with entity {entity_id}: {e}")
                return False, entity_id

    async def unsubscribe_dead_bots(self):
        """Unsubscribe from dead bots with improved concurrency."""
        path = self.storage._get_path("dead_bots.txt")
        if not os.path.exists(path):
            logger.error(f"{EMOJI_ERROR} dead_bots.txt not found. Please scan for dead bots first.")
            return []

        with open(path, "r", encoding='utf-8') as f:
            usernames = [line.strip().lstrip("@") for line in f if line.strip()]

        logger.info(f"{EMOJI_INFO} Processing {len(usernames)} dead bots...")

        client = await self.get_client()
        progress = ProgressTracker(len(usernames), "Unsubscribing")

        processed_bots = []

        async def process_bot(username):
            """Process a single bot."""
            try:
                entity = await self.get_entity(username)

                if hasattr(entity, 'megagroup') or hasattr(entity, 'broadcast'):
                    await self.rate_limited_request(client, LeaveChannelRequest(entity))
                    logger.info(f"{EMOJI_SUCCESS} Left channel/group @{username}")
                else:
                    await self.rate_limited_request(client, DeleteHistoryRequest(peer=entity, max_id=0, revoke=True))
                    logger.info(f"{EMOJI_SUCCESS} Deleted chat with @{username}")

                progress.update(True)
                return True, username
            except Exception as e:
                logger.error(f"{EMOJI_ERROR} Error with @{username}: {e}")
                progress.update(False)
                return False, username

        # Process bots in batches to avoid hitting rate limits
        batch_size = 10
        for i in range(0, len(usernames), batch_size):
            batch = usernames[i:i + batch_size]

            # Create and run tasks for this batch
            tasks = [process_bot(username) for username in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            # Process results
            for result in batch_results:
                if isinstance(result, Exception):
                    logger.error(f"{EMOJI_ERROR} Batch task exception: {result}")
                    continue

                success, username = result
                if success:
                    processed_bots.append(username)

            # Wait between batches
            if i + batch_size < len(usernames):
                await asyncio.sleep(5)

        progress.complete()
        logger.info(f"{EMOJI_INFO} Unsubscribe process complete! Processed {len(processed_bots)} bots")

        return processed_bots

    async def delete_deleted_account_chats(self):
        """Delete chats of deleted accounts with progress tracking."""
        users = self.storage.load_deleted_accounts()
        if not users:
            logger.error(f"{EMOJI_ERROR} No deleted accounts found. Please scan for deleted accounts first.")
            return []

        client = await self.get_client()
        progress = ProgressTracker(len(users), "Deleting chats")

        deleted_chats = []

        for user in users:
            user_id = user.user_id
            try:
                entity = await self.get_entity(user)

                if not entity.deleted:
                    logger.warning(f"{EMOJI_WARNING} Skipping user_id={user_id}: Not deleted")
                    progress.update(False)
                    continue

                await self.rate_limited_request(client, DeleteHistoryRequest(peer=user, max_id=0, revoke=True))
                logger.info(f"{EMOJI_SUCCESS} Deleted chat for user_id={user_id}")
                deleted_chats.append(user_id)
                progress.update(True)

                # Small delay between operations
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"{EMOJI_ERROR} Error with user_id={user_id}: {e}")
                progress.update(False)

        progress.complete()
        logger.info(f"{EMOJI_INFO} Chat deletion complete! Deleted {len(deleted_chats)} chats")

        return deleted_chats

    async def cleanup_files(self):
        """Delete temporary files."""
        files_to_delete = ["deleted_accounts.txt", "dead_bots.txt", "seen_bots.txt"]
        logger.warning(f"{EMOJI_WARNING} Cleaning up files: {', '.join(files_to_delete)}")

        deleted_files = []

        for file in files_to_delete:
            path = self.storage._get_path(file)
            if os.path.exists(path):
                os.remove(path)
                logger.info(f"{EMOJI_SUCCESS} Deleted {file}")
                deleted_files.append(file)
            else:
                logger.info(f"{EMOJI_INFO} {file} does not exist")

        logger.info(f"{EMOJI_INFO} Cleanup complete! Deleted {len(deleted_files)} files")
        return deleted_files


class TelegramCleanerCLI:
    """Command-line interface for the Telegram cleaner."""

    def __init__(self):
        self.cleaner = TelegramCleaner()

    def clear_screen(self):
        """Clear the console screen."""
        os.system('cls' if os.name == 'nt' else 'clear')

    def print_menu(self):
        """Display the CLI menu."""
        self.clear_screen()
        print(f"{COLOR_BLUE}{EMOJI_MENU} === Telegram Cleaner Menu === {COLOR_RESET}")
        print(f"1. {EMOJI_INFO} Scan for dead bots")
        print(f"2. {EMOJI_INFO} Scan for deleted accounts")
        print(f"3. {EMOJI_SUCCESS} Unsubscribe from dead bots")
        print(f"4. {EMOJI_SUCCESS} Delete chats of deleted accounts")
        print(f"5. {EMOJI_CLEANUP} Clean up temporary files")
        print(f"6. {EMOJI_ERROR} Exit")
        print(f"{COLOR_BLUE}============================={COLOR_RESET}")

    async def run(self):
        """Main CLI loop with menu."""
        await self.cleaner.initialize()

        while True:
            self.print_menu()
            choice = input(f"{COLOR_YELLOW}Enter your choice (1-6): {COLOR_RESET}")

            try:
                if choice == "1":
                    await self.cleaner.scan_dead_bots()
                elif choice == "2":
                    await self.cleaner.scan_deleted_accounts()
                elif choice == "3":
                    await self.cleaner.unsubscribe_dead_bots()
                elif choice == "4":
                    await self.cleaner.delete_deleted_account_chats()
                elif choice == "5":
                    await self.cleaner.cleanup_files()
                elif choice == "6":
                    print(f"{COLOR_GREEN}{EMOJI_INFO} Goodbye! Thanks for using Telegram Cleaner.{COLOR_RESET}")
                    break
                else:
                    print(f"{COLOR_RED}{EMOJI_ERROR} Invalid choice. Please select 1-6.{COLOR_RESET}")
            except Exception as e:
                logger.error(f"{COLOR_RED}{EMOJI_ERROR} An error occurred: {e}{COLOR_RESET}")
                import traceback
                traceback.print_exc()

            input(f"{COLOR_BLUE}{EMOJI_INFO} Press Enter to continue...{COLOR_RESET}")

        await self.cleaner.close_client()


async def main():
    """Main entry point."""
    cli = TelegramCleanerCLI()
    await cli.run()


if __name__ == "__main__":
    asyncio.run(main())