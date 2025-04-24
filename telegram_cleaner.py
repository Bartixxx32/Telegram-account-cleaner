#!/usr/bin/env python3
# telegram_cleaner_optimized.py

import asyncio
import os
import time
import json
import logging
import sys
import locale
import random
import codecs
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from aiolimiter import AsyncLimiter
from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, PeerIdInvalidError, UserIdInvalidError
from telethon.tl.functions.messages import DeleteHistoryRequest
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.types import User, InputUser, Channel, Chat

# -------------------------
# UTF-8 Enforcement
# -------------------------

def force_utf8_streams():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        try:
            sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer)
            sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer)
        except Exception:
            pass

force_utf8_streams()

# -------------------------
# Logging Setup
# -------------------------

class SafeStreamHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            super().emit(record)
        except UnicodeEncodeError:
            record.msg = record.getMessage().encode('ascii', 'replace').decode('ascii')
            record.args = ()
            super().emit(record)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('telegram_cleaner.log', encoding='utf-8'),
        SafeStreamHandler()
    ]
)
logger = logging.getLogger('TelegramCleaner')

# -------------------------
# Emojis & Colors
# -------------------------

EMOJI_SUCCESS = '✅'; EMOJI_ERROR = '❌'; EMOJI_WARNING = '⚠️'
EMOJI_INFO = 'ℹ️'; EMOJI_MENU = '📋'; EMOJI_CLEANUP = '🧹'
COLOR_GREEN = '\033[92m'; COLOR_RED = '\033[91m'
COLOR_YELLOW = '\033[93m'; COLOR_BLUE = '\033[94m'; COLOR_RESET = '\033[0m'

def safe_print(text: str):
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode('ascii', 'replace').decode('ascii'))

# -------------------------
# Data Storage
# -------------------------

class DataStorage:
    def __init__(self, data_dir: str = 'data'):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

    def path(self, filename: str) -> str:
        return os.path.join(self.data_dir, filename)

    def load_set(self, filename: str) -> Set[str]:
        p = self.path(filename)
        if not os.path.exists(p): return set()
        with open(p, 'r', encoding='utf-8') as f:
            return set(line.strip() for line in f if line.strip())

    def save_set(self, filename: str, data: Set[str]):
        with open(self.path(filename), 'w', encoding='utf-8') as f:
            for x in sorted(data): f.write(f"{x}\n")

    def load_dict(self, filename: str) -> Dict:
        p = self.path(filename)
        if not os.path.exists(p): return {}
        try:
            with open(p, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.error(f"JSON decode error in {filename}")
            return {}

    def save_dict(self, filename: str, data: Dict):
        with open(self.path(filename), 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

    def load_deleted_accounts(self) -> List[InputUser]:
        users=[]; p=self.path('deleted_accounts.txt')
        if os.path.exists(p):
            with open(p,'r',encoding='utf-8') as f:
                for line in f:
                    uid, ah = line.strip().split(',')[:2]
                    users.append(InputUser(int(uid), int(ah)))
        return users

    def save_credentials(self, api_id: str, api_hash: str):
        with open(self.path('credentials.txt'),'w') as f:
            f.write(f"{api_id},{api_hash}")

    def load_credentials(self) -> Tuple[Optional[str], Optional[str]]:
        p=self.path('credentials.txt')
        if not os.path.exists(p): return None,None
        with open(p,'r') as f:
            parts=f.read().strip().split(',');
            return (parts[0], parts[1]) if len(parts)==2 else (None,None)

# -------------------------
# Rate Limiter (Token Bucket + Jitter)
# -------------------------

class RateLimiter:
    def __init__(self, max_rate:int=20, period:float=60.0, max_delay:float=64.0):
        self.limiter=AsyncLimiter(max_rate,period)
        self.delay=1.0; self.max_delay=max_delay; self.last_error:Optional[float]=None

    async def wait_if_needed(self):
        await self.limiter.acquire()
        if self.last_error and (time.time()-self.last_error)<self.delay*10:
            jitter=random.uniform(0.5,1.5)
            logger.info(f"{EMOJI_INFO} Backoff {self.delay:.2f}s×{jitter:.2f}")
            await asyncio.sleep(self.delay*jitter)

    def record_success(self): self.delay=max(1.0,self.delay*0.9)
    def record_error(self, wait:Optional[float]=None):
        self.last_error=time.time()
        self.delay=min(self.max_delay, (wait or self.delay)*1.5)

# -------------------------
# Progress Tracker
# -------------------------

class ProgressTracker:
    def __init__(self,total:int,desc:str="Processing"):
        self.total=total; self.desc=desc; self.curr=0; self.start=time.time()
    def update(self):
        self.curr+=1; pct=self.curr/self.total*100 if self.total else 100
        elapsed=time.time()-self.start
        rate=self.curr/elapsed if elapsed>0 else 0
        eta=(self.total-self.curr)/rate if rate>0 else 0
        bar='#'*int(pct//3)+'-'*(30-int(pct//3))
        print(f"\r{self.desc}: [{bar}] {pct:.1f}% ({self.curr}/{self.total}) ETA:{eta:.1f}s",end='')
        if self.curr==self.total: print()

# -------------------------
# Entity Cache
# -------------------------

class EntityCache:
    def __init__(self, max_age:int=3600):
        self.cache:Dict[str,Tuple[Any,float]]={}; self.max_age=max_age
    def get(self,key:str)->Any:
        if key in self.cache:
            ent,ts=self.cache[key]
            if time.time()-ts<self.max_age: return ent
            del self.cache[key]
        return None
    def set(self,key:str,ent:Any): self.cache[key]=(ent,time.time())
    def invalidate(self,key:str=None):
        if key: self.cache.pop(key,None)
        else: self.cache.clear()

# -------------------------
# Telegram Cleaner
# -------------------------

class TelegramCleaner:
    def __init__(self,concurrency:int=10):
        self.storage=DataStorage(); self.limiter=RateLimiter();
        self.client:Optional[TelegramClient]=None; self.api_id=None; self.api_hash=None
        self.sem=asyncio.Semaphore(concurrency); self.cache=EntityCache()

    async def initialize(self):
        self.api_id,self.api_hash=self.storage.load_credentials()
        if not self.api_id or not self.api_hash:
            safe_print(f"{COLOR_BLUE}{EMOJI_INFO} Enter API ID & Hash:{COLOR_RESET}")
            self.api_id=input("API ID: "); self.api_hash=input("API Hash: ")
            self.storage.save_credentials(self.api_id,self.api_hash)
            safe_print(f"{COLOR_GREEN}{EMOJI_SUCCESS} Saved credentials.{COLOR_RESET}")

    async def get_client(self):
        if not self.client:
            self.client=TelegramClient('session',self.api_id,self.api_hash)
            await self.client.start()
        return self.client

    async def do_request(self,fn,*args,**kw):
        await self.limiter.wait_if_needed()
        try:
            res=await fn(*args,**kw); self.limiter.record_success(); return res
        except FloodWaitError as e:
            self.limiter.record_error(e.seconds)
            delay=e.seconds*random.uniform(0.8,1.2)
            logger.warning(f"{EMOJI_WARNING} Flood wait {delay:.1f}s")
            await asyncio.sleep(delay);
            return await self.do_request(fn,*args,**kw)
        except Exception:
            self.limiter.record_error()
            raise

    async def get_entity(self,ident):
        key=str(ident); ent=self.cache.get(key)
        if ent: return ent
        client=await self.get_client()
        ent=await self.do_request(client.get_entity,ident)
        self.cache.set(key,ent); return ent

    async def scan_dead_bots(self)->Set[str]:
        seen=self.storage.load_set('seen_bots.txt'); dead=self.storage.load_set('dead_bots.txt')
        client=await self.get_client(); dialogs=await self.do_request(client.get_dialogs,limit=None)
        bots=[d.entity for d in dialogs if isinstance(d.entity,User) and d.entity.bot]
        new=[b for b in bots if (b.username or str(b.id)) not in seen]
        if not new: logger.info(f"{EMOJI_INFO} No new bots."); return dead
        prog=ProgressTracker(len(new),'Pinging bots')
        async def ping(b):
            async with self.sem:
                uname=b.username or str(b.id)
                try:
                    await self.do_request(client.send_message,b,'/start'); await asyncio.sleep(1)
                    msgs=await self.do_request(client.get_messages,b,limit=3)
                    alive=any(m.message and '/start' not in m.message for m in msgs)
                    seen.add(uname);
                    if not alive: dead.add(uname)
                except Exception:
                    seen.add(uname); dead.add(uname)
                prog.update()
        await asyncio.gather(*(ping(b) for b in new))
        self.storage.save_set('seen_bots.txt',seen); self.storage.save_set('dead_bots.txt',dead)
        return dead

    async def scan_deleted_accounts(self)->List[InputUser]:
        client=await self.get_client(); dialogs=await self.do_request(client.get_dialogs,limit=None)
        users=[d.entity for d in dialogs if isinstance(d.entity,User) and not d.entity.bot]
        deleted=[u for u in users if u.deleted]
        with open(self.storage.path('deleted_accounts.txt'),'w',encoding='utf-8') as f:
            for u in deleted: f.write(f"{u.id},{u.access_hash}\n")
        return deleted

    async def unsubscribe_dead_bots(self)->List[str]:
        names=list(self.storage.load_set('dead_bots.txt'))
        if not names: logger.error(f"{EMOJI_ERROR} No dead bots."); return []
        client=await self.get_client()
        ents=await self.do_request(client.get_entity,names)
        prog=ProgressTracker(len(ents),'Unsubscribing')
        left=[]
        async def leave(e):
            async with self.sem:
                try:
                    if hasattr(e,'megagroup') or hasattr(e,'broadcast'):
                        await self.do_request(client(LeaveChannelRequest(e)))
                    else:
                        await self.do_request(client(DeleteHistoryRequest(peer=e,max_id=0,revoke=True)))
                    left.append(getattr(e,'username',str(e.id)))
                except Exception: pass
                prog.update()
        await asyncio.gather(*(leave(e) for e in ents)); return left

    async def delete_deleted_account_chats(self)->List[int]:
        users=self.storage.load_deleted_accounts(); client=await self.get_client()
        prog=ProgressTracker(len(users),'Deleting chats'); removed=[]
        for u in users:
            try: await self.do_request(client.delete_messages,u,None,revoke=True); removed.append(u.user_id)
            except: pass
            prog.update(); await asyncio.sleep(1)
        return removed

    async def cleanup_files(self)->List[str]:
        files=['deleted_accounts.txt','dead_bots.txt','seen_bots.txt']; deleted=[]
        for f in files:
            p=self.storage.path(f)
            if os.path.exists(p): os.remove(p); deleted.append(f)
        return deleted

# -------------------------
# CLI
# -------------------------

class TelegramCleanerCLI:
    def __init__(self): self.cleaner=TelegramCleaner(concurrency=15)
    def clear(self): os.system('cls' if os.name=='nt' else 'clear')
    def menu(self):
        self.clear(); safe_print(f"{COLOR_BLUE}{EMOJI_MENU} Telegram Cleaner Menu{COLOR_RESET}")
        safe_print("1. Scan dead bots"); safe_print("2. Scan deleted accounts")
        safe_print("3. Unsubscribe dead bots"); safe_print("4. Delete chats of deleted accounts")
        safe_print("5. Cleanup files"); safe_print("6. Exit")
    async def run(self):
        await self.cleaner.initialize()
        while True:
            self.menu(); choice=input(f"{COLOR_YELLOW}Choice(1-6): {COLOR_RESET}")
            try:
                if choice=='1': dead=await self.cleaner.scan_dead_bots(); print(f"Dead bots: {len(dead)}")
                elif choice=='2': dels=await self.cleaner.scan_deleted_accounts(); print(f"Deleted accounts: {len(dels)}")
                elif choice=='3': left=await self.cleaner.unsubscribe_dead_bots(); print(f"Unsubscribed: {len(left)}")
                elif choice=='4': rem=await self.cleaner.delete_deleted_account_chats(); print(f"Removed chats: {len(rem)}")
                elif choice=='5': cln=await self.cleaner.cleanup_files(); print(f"Cleaned: {cln}")
                elif choice=='6': safe_print(f"{COLOR_GREEN}Goodbye!{COLOR_RESET}"); break
            except Exception as e:
                logger.error(f"Error: {e}"); import traceback; traceback.print_exc()
            input(f"{COLOR_BLUE}Press Enter...{COLOR_RESET}")

async def main(): cli=TelegramCleanerCLI(); await cli.run()
if __name__=='__main__': asyncio.run(main())

