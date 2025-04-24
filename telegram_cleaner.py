import asyncio
import os
import time
from telethon.sync import TelegramClient
from telethon.tl.functions.messages import DeleteHistoryRequest
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.types import User, InputUser
from telethon.errors import FloodWaitError, PeerIdInvalidError, UserIdInvalidError

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
session_name = "telegram_cleaner"

def clear_screen():
    """Clear the console screen."""
    os.system('cls' if os.name == 'nt' else 'clear')

def print_menu():
    """Display the CLI menu."""
    clear_screen()
    print(f"{COLOR_BLUE}{EMOJI_MENU} === Telegram Cleaner Menu === {COLOR_RESET}")
    print(f"1. {EMOJI_INFO} Scan for dead bots")
    print(f"2. {EMOJI_INFO} Scan for deleted accounts")
    print(f"3. {EMOJI_SUCCESS} Unsubscribe from dead bots")
    print(f"4. {EMOJI_SUCCESS} Delete chats of deleted accounts")
    print(f"5. {EMOJI_CLEANUP} Clean up temporary files")
    print(f"6. {EMOJI_ERROR} Exit")
    print(f"{COLOR_BLUE}============================={COLOR_RESET}")

def load_set(filename):
    """Load a set of items from a file."""
    if os.path.exists(filename):
        with open(filename, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_set(filename, data_set):
    """Save a set of items to a file."""
    with open(filename, "w") as f:
        for item in sorted(data_set):
            f.write(f"{item}\n")

def load_deleted_accounts(filename="deleted_accounts.txt"):
    """Load user IDs and access hashes from file."""
    users = []
    try:
        with open(filename, "r", encoding='utf-8') as f:
            for line in [line.strip() for line in f if line.strip()]:
                try:
                    uid, access_hash = line.split(",")[:2]
                    users.append(InputUser(int(uid), int(access_hash)))
                except ValueError:
                    print(f"{COLOR_RED}{EMOJI_ERROR} Invalid line in {filename}: {line}{COLOR_RESET}")
    except FileNotFoundError:
        print(f"{COLOR_RED}{EMOJI_ERROR} {filename} not found{COLOR_RESET}")
    return users

async def scan_dead_bots():
    """Scan for dead bots and save results to dead_bots.txt."""
    seen_bots = load_set("seen_bots.txt")
    dead_bots = load_set("dead_bots.txt")
    async with TelegramClient(session_name, api_id, api_hash) as client:
        print(f"{COLOR_BLUE}{EMOJI_INFO} Fetching your Telegram dialogs...{COLOR_RESET}")
        dialogs = await client.get_dialogs(limit=None)
        bot_users = [d.entity for d in dialogs if isinstance(d.entity, User) and d.entity.bot]
        new_bots = [b for b in bot_users if (b.username or str(b.id)) not in seen_bots]
        print(f"{COLOR_BLUE}{EMOJI_INFO} Found {len(bot_users)} bots, scanning {len(new_bots)} new ones...{COLOR_RESET}")

        for bot in new_bots:
            username = bot.username or str(bot.id)
            seen_bots.add(username)
            try:
                print(f"{COLOR_YELLOW}⏳ Pinging @{username}{COLOR_RESET}")
                await client.send_message(bot, '/start')
                await asyncio.sleep(1.5)
                messages = await client.get_messages(bot, limit=3)
                if not messages or all("/start" in m.message for m in messages):
                    dead_bots.add(username)
                    print(f"{COLOR_RED}{EMOJI_ERROR} No response from @{username}{COLOR_RESET}")
                else:
                    print(f"{COLOR_GREEN}{EMOJI_SUCCESS} @{username} responded{COLOR_RESET}")
            except FloodWaitError as e:
                print(f"{COLOR_YELLOW}{EMOJI_WARNING} Rate limit hit, waiting {e.seconds}s...{COLOR_RESET}")
                time.sleep(e.seconds + 1)
            except Exception as e:
                print(f"{COLOR_RED}{EMOJI_ERROR} Error with @{username}: {e}{COLOR_RESET}")
                dead_bots.add(username)
    save_set("seen_bots.txt", seen_bots)
    save_set("dead_bots.txt", dead_bots)
    print(f"{COLOR_GREEN}{EMOJI_INFO} Scan complete! Total dead bots: {len(dead_bots)}{COLOR_RESET}")
    input(f"{COLOR_BLUE}{EMOJI_INFO} Press Enter to return to menu...{COLOR_RESET}")

async def scan_deleted_accounts():
    """Scan for deleted accounts and save to deleted_accounts.txt."""
    async with TelegramClient(session_name, api_id, api_hash) as client:
        print(f"{COLOR_BLUE}{EMOJI_INFO} Scanning your Telegram dialogs...{COLOR_RESET}")
        dialogs = await client.get_dialogs(limit=None)
        contacts = [d.entity for d in dialogs if isinstance(d.entity, User) and not d.entity.bot]
        deleted_users = [user for user in contacts if user.deleted]
        print(f"{COLOR_YELLOW}{EMOJI_INFO} Found {len(deleted_users)} deleted accounts{COLOR_RESET}")

        with open("deleted_accounts.txt", "w") as f:
            for user in deleted_users:
                f.write(f"{user.id},{user.access_hash}\n")
        print(f"{COLOR_GREEN}{EMOJI_SUCCESS} Saved {len(deleted_users)} deleted accounts to deleted_accounts.txt{COLOR_RESET}")
    input(f"{COLOR_BLUE}{EMOJI_INFO} Press Enter to return to menu...{COLOR_RESET}")

async def unsubscribe_dead_bots():
    """Unsubscribe from dead bots listed in dead_bots.txt."""
    try:
        with open("dead_bots.txt", "r", encoding='utf-8') as f:
            usernames = [line.strip().lstrip("@") for line in f if line.strip()]
        print(f"{COLOR_BLUE}{EMOJI_INFO} Loaded {len(usernames)} dead bots to unsubscribe from{COLOR_RESET}")
    except FileNotFoundError:
        print(f"{COLOR_RED}{EMOJI_ERROR} dead_bots.txt not found. Please scan for dead bots first.{COLOR_RESET}")
        input(f"{COLOR_BLUE}{EMOJI_INFO} Press Enter to return to menu...{COLOR_RESET}")
        return

    async with TelegramClient(session_name, api_id, api_hash) as client:
        for username in usernames:
            name = f"@{username}" if username.isalnum() else username
            try:
                entity = await client.get_entity(username)
                if hasattr(entity, 'megagroup') or hasattr(entity, 'broadcast'):
                    await client(LeaveChannelRequest(entity))
                    print(f"{COLOR_GREEN}{EMOJI_SUCCESS} Left channel/group {name}{COLOR_RESET}")
                else:
                    await client(DeleteHistoryRequest(peer=entity, max_id=0, revoke=True))
                    print(f"{COLOR_GREEN}{EMOJI_SUCCESS} Deleted chat with {name}{COLOR_RESET}")
                await asyncio.sleep(1.5)
            except FloodWaitError as e:
                print(f"{COLOR_YELLOW}{EMOJI_WARNING} Rate limit hit, waiting {e.seconds}s...{COLOR_RESET}")
                time.sleep(e.seconds + 1)
            except PeerIdInvalidError:
                print(f"{COLOR_RED}{EMOJI_ERROR} Could not find {name}{COLOR_RESET}")
            except Exception as e:
                print(f"{COLOR_RED}{EMOJI_ERROR} Error with {name}: {e}{COLOR_RESET}")
        print(f"{COLOR_GREEN}{EMOJI_INFO} Unsubscribe process complete!{COLOR_RESET}")
    input(f"{COLOR_BLUE}{EMOJI_INFO} Press Enter to return to menu...{COLOR_RESET}")

async def delete_deleted_account_chats():
    """Delete chats of deleted accounts from deleted_accounts.txt."""
    users = load_deleted_accounts()
    if not users:
        print(f"{COLOR_RED}{EMOJI_ERROR} No deleted accounts found. Please scan for deleted accounts first.{COLOR_RESET}")
        input(f"{COLOR_BLUE}{EMOJI_INFO} Press Enter to return to menu...{COLOR_RESET}")
        return

    async with TelegramClient(session_name, api_id, api_hash) as client:
        print(f"{COLOR_BLUE}{EMOJI_INFO} Processing {len(users)} deleted accounts...{COLOR_RESET}")
        for user in users:
            user_id = user.user_id
            try:
                entity = await client.get_entity(user)
                if not entity.deleted:
                    print(f"{COLOR_YELLOW}{EMOJI_WARNING} Skipping user_id={user_id}: Not deleted{COLOR_RESET}")
                    continue
                await client(DeleteHistoryRequest(peer=user, max_id=0, revoke=True))
                print(f"{COLOR_GREEN}{EMOJI_SUCCESS} Deleted chat for user_id={user_id}{COLOR_RESET}")
                await asyncio.sleep(1.5)
            except UserIdInvalidError:
                print(f"{COLOR_RED}{EMOJI_ERROR} Invalid user_id={user_id}{COLOR_RESET}")
            except FloodWaitError as e:
                print(f"{COLOR_YELLOW}{EMOJI_WARNING} Rate limit hit, waiting {e.seconds}s...{COLOR_RESET}")
                time.sleep(e.seconds + 1)
            except Exception as e:
                print(f"{COLOR_RED}{EMOJI_ERROR} Error with user_id={user_id}: {e}{COLOR_RESET}")
        print(f"{COLOR_GREEN}{EMOJI_INFO} Chat deletion complete!{COLOR_RESET}")
    input(f"{COLOR_BLUE}{EMOJI_INFO} Press Enter to return to menu...{COLOR_RESET}")

async def cleanup_files():
    """Delete temporary files like deleted_accounts.txt, dead_bots.txt, etc."""
    files_to_delete = ["deleted_accounts.txt", "dead_bots.txt", "seen_bots.txt"]
    print(f"{COLOR_YELLOW}{EMOJI_WARNING} Are you sure you want to delete the following files?{COLOR_RESET}")
    for file in files_to_delete:
        print(f" - {file}")
    confirm = input(f"{COLOR_YELLOW}Type 'y' to confirm, 'n' to cancel: {COLOR_RESET}").lower()
    if confirm == 'y':
        for file in files_to_delete:
            if os.path.exists(file):
                os.remove(file)
                print(f"{COLOR_GREEN}{EMOJI_SUCCESS} Deleted {file}{COLOR_RESET}")
            else:
                print(f"{COLOR_YELLOW}{EMOJI_INFO} {file} does not exist{COLOR_RESET}")
        print(f"{COLOR_GREEN}{EMOJI_INFO} Cleanup complete!{COLOR_RESET}")
    else:
        print(f"{COLOR_YELLOW}{EMOJI_INFO} Cleanup canceled.{COLOR_RESET}")
    input(f"{COLOR_BLUE}{EMOJI_INFO} Press Enter to return to menu...{COLOR_RESET}")

async def main():
    """Main CLI loop with menu."""
    # Load or prompt for API credentials
    global api_id, api_hash
    if os.path.exists("credentials.txt"):
        with open("credentials.txt", "r") as f:
            api_id, api_hash = f.read().strip().split(",")
        print(f"{COLOR_BLUE}{EMOJI_INFO} Loaded API credentials from file.{COLOR_RESET}")
    else:
        print(f"{COLOR_BLUE}{EMOJI_INFO} Please enter your Telegram API credentials.{COLOR_RESET}")
        api_id = input("API ID: ")
        api_hash = input("API Hash: ")
        with open("credentials.txt", "w") as f:
            f.write(f"{api_id},{api_hash}")
        print(f"{COLOR_GREEN}{EMOJI_SUCCESS} Saved API credentials to credentials.txt.{COLOR_RESET}")

    while True:
        print_menu()
        choice = input(f"{COLOR_YELLOW}Enter your choice (1-6): {COLOR_RESET}")
        if choice == "1":
            await scan_dead_bots()
        elif choice == "2":
            await scan_deleted_accounts()
        elif choice == "3":
            await unsubscribe_dead_bots()
        elif choice == "4":
            await delete_deleted_account_chats()
        elif choice == "5":
            await cleanup_files()
        elif choice == "6":
            print(f"{COLOR_GREEN}{EMOJI_INFO} Goodbye! Thanks for using Telegram Cleaner.{COLOR_RESET}")
            break
        else:
            print(f"{COLOR_RED}{EMOJI_ERROR} Invalid choice. Please select 1-6.{COLOR_RESET}")
            input(f"{COLOR_BLUE}{EMOJI_INFO} Press Enter to continue...{COLOR_RESET}")

if __name__ == "__main__":
    asyncio.run(main())