import asyncio
import json
import secrets
import logging
import re
import time
from datetime import datetime
from urllib import request, error
from zoneinfo import ZoneInfo

CAIRO = ZoneInfo("Africa/Cairo")
ICONS = dict(status="5071491301443110142", add="5278760062301522159",
             groups="5453957997418004470", issues="5890996942992840314",
             blocked="6086741365998227951", pause="6052878855994089809",
             resume="6053202133887490757", delete="5372825386591732174",
             back="5440509136259267820", yes="5460960662421257616",
             no="5278299040511970900", clock="5780543148782522693",
             refresh="5244758760429213978")

def btn(label, action, icon, style="primary"):
    return dict(text=label, callback_data=action, style=style, icon_custom_emoji_id=ICONS[icon])

BACK = btn("العودة إلى القائمة", "home", "back")

def when(t):
    return datetime.fromtimestamp(t, CAIRO).strftime("%d/%m %H:%M")

def duration(seconds):
    minutes, sec = divmod(int(seconds), 60)
    return f"{minutes} دقيقة" + (f" و{sec} ثانية" if sec else "")

class State:
    def __init__(self, db, account, targets, interval):
        self.db, self.account = db, account
        db.executescript("""
        CREATE TABLE IF NOT EXISTS settings(account INTEGER, k TEXT, v TEXT, PRIMARY KEY(account,k));
        CREATE TABLE IF NOT EXISTS targets(id INTEGER PRIMARY KEY, account INTEGER, k TEXT, title TEXT, UNIQUE(account,k));
        """)
        with db:
            if self.get("interval") is None:
                for k in targets:
                    self.add(k, k)
                self.set("interval", interval)
        self.changed = asyncio.Event()

    def get(self, k, default=None):
        row = self.db.execute("SELECT v FROM settings WHERE account=? AND k=?", (self.account,k)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?,?)", (self.account,k,json.dumps(v)))

    def add(self, k, title):
        self.db.execute("INSERT OR IGNORE INTO targets(account,k,title) VALUES(?,?,?)", (self.account,k,title))

    def groups(self):
        return list(self.db.execute("SELECT id,k,title FROM targets WHERE account=? ORDER BY id", (self.account,)))

    async def wait(self, seconds=None):
        self.changed.clear()
        try:
            await asyncio.wait_for(self.changed.wait(), seconds)
        except asyncio.TimeoutError:
            pass


class Panel:
    def __init__(self, token, admin, state, validate, blocked):
        self.url = "https://api.telegram.org/bot" + token + "/"
        self.admin, self.s, self.validate, self.blocked = admin, state, validate, blocked
        self.pending = self.input = None
        self.scope = f"panel:{admin}:{token.partition(':')[0]}"
        self.mid = state.get(self.scope)
        self.cleanup_key = self.scope + ":cleanup"
        self.plain = False

    async def api(self, method, **data):
        def post():
            req = request.Request(self.url + method, json.dumps(data).encode(),
                                  headers={"Content-Type":"application/json"})
            try:
                with request.urlopen(req, timeout=40) as response:
                    return json.load(response)["result"]
            except error.HTTPError as exc:
                raise RuntimeError(json.loads(exc.read()).get("description","Bot API error")) from None
        return await asyncio.to_thread(post)

    async def show(self, text, rows):
        data = dict(chat_id=self.admin, text=text, reply_markup={"inline_keyboard":rows})
        for _ in range(3):
            if self.plain:
                data["reply_markup"] = {"inline_keyboard":[[{k:v for k,v in b.items()
                    if k != "icon_custom_emoji_id"} for b in row] for row in rows]}
            try:
                if self.mid:
                    await self.api("editMessageText", message_id=self.mid, **data)
                else:
                    self.mid = (await self.api("sendMessage", **data))["message_id"]
                    with self.s.db:
                        self.s.set(self.scope, self.mid)
                return
            except RuntimeError as exc:
                msg = str(exc).lower()
                if "not modified" in msg:
                    return
                if "emoji" in msg and not self.plain:
                    self.plain = True
                elif self.mid and ("not found" in msg or "can't be edited" in msg):
                    self.mid = None
                else:
                    raise

    async def confirm(self, text, op, arg=None):
        nonce = secrets.token_hex(6)
        self.pending = (nonce,op,arg)
        await self.show(text, [[btn("تأكيد","yes:"+nonce,"yes","success"),btn("إلغاء","home","no","danger")]])

    def timing(self):
        s = self.s
        if s.get("paused"):
            return "الإرسال متوقف مؤقتًا."
        if s.get("active_general"):
            return "الجولة العامة جارية. يبدأ احتساب الفاصل عند انتهائها."
        due = s.get("due_general",time.time())
        return f"الجولة العامة القادمة {when(due)} بتوقيت مصر، بعد {max(0,int(due-time.time()+59)//60)} دقيقة."

    async def screen(self, action):
        s = self.s
        notice = ""
        pending, self.pending, self.input = self.pending, None, None
        if action.startswith("yes:"):
            if not pending or action[4:] != pending[0]:
                return await self.show("انتهت صلاحية هذا التأكيد. افتح الإجراء من جديد.", [[BACK]])
            _,op,arg = pending
            with s.db:
                if op == "interval":
                    s.set("interval",arg)
                    s.set("due_general",time.time()+arg)
                elif op in ("resume","pause"):
                    s.set("paused",op=="pause")
                elif op == "add":
                    for k,title in arg:
                        s.add(k,title)
                elif op == "delete":
                    s.db.execute("DELETE FROM targets WHERE account=? AND id=?", (s.account,arg))
                elif op == "restore":
                    s.db.execute("DELETE FROM blacklist WHERE account=? AND target=?", (s.account,arg))
                    self.blocked.discard(arg)
            s.changed.set()
            notice = "تم حفظ التغيير.\n"
            action = "home"
        if action == "home":
            paused = s.get("paused",False)
            await self.show(notice+"لوحة إدارة الإرسال\n"+self.timing(), [
                [btn("حالة الإرسال","status","status"),btn("توقيت الإرسال","clock","clock")],
                [btn("المجموعات","groups:0","groups"),btn("إضافة مجموعات","add","add","success")],
                [btn("تفاصيل التعثر","issues:0","issues"),btn("القائمة السوداء","blocked:0","blocked","danger")],
                [btn("استئناف الإرسال","resume","resume","success") if paused else btn("إيقاف مؤقت","pause","pause","danger")]])
        elif action == "clock":
            await self.show(f"توقيت الإرسال العام\nالفاصل الحالي بين نهاية الجولة وبداية التالية {duration(s.get('interval'))}.\n"+self.timing(),
                            [[btn("تغيير الفاصل الزمني","change","clock")],[BACK]])
        elif action == "change":
            await self.show("اختر مدة الانتظار بعد انتهاء الجولة العامة.", [
                [btn(label,"time:"+str(n),"clock") for label,n in (("ساعة",60),("ساعة وربع",75))],
                [btn(label,"time:"+str(n),"clock") for label,n in (("ساعة ونصف",90),("ساعتان",120))],
                [btn("إدخال عدد الدقائق","custom","clock")],[BACK]])
        elif action.startswith("time:"):
            secs = int(action.split(":")[1])*60
            detail = ("تبدأ المدة عند انتهاء الجولة الحالية." if s.get("active_general") else
                      f"إذا أكّدت الآن، يحين الموعد في {when(time.time()+secs)} بتوقيت مصر.")
            await self.confirm(f"هل تريد تغيير الفاصل من {duration(s.get('interval'))} إلى {duration(secs)}؟\n{detail}\nيسري الإرسال عند الاستئناف إذا كان متوقفًا.", "interval", secs)
        elif action in ("custom","add"):
            self.input = action
            await self.show("أرسل عدد الدقائق من ١ إلى ٥٢٥٦٠٠." if action=="custom" else
                            "أرسل أسماء المجموعات أو روابطها أو معرّفاتها، وافصل بينها بفاصلة أو سطر جديد. ستراجع أسماءها قبل إضافتها.", [[BACK]])
        elif action in ("pause","resume"):
            await self.confirm("هل تريد إيقاف الجولتين مؤقتًا؟ ستكتمل محاولة الإرسال التي بدأت بالفعل." if action=="pause" else
                               "هل تريد استئناف الإرسال ومتابعة الجولة من موضع التوقف؟",action)
        elif action == "status":
            lines = ["حالة الإرسال",self.timing()]
            for key,label in (("general","العامة"),("special","الخاصة")):
                r = s.get("active_"+key) or s.get("last_"+key)
                if r:
                    lines.append(f"الجولة {label} · نجح {r['ok']}، فشل {r['fail']}، تم تخطي {r['skip']}، متبقٍ {len(r['targets'])-r['index']}.")
                if key=="special" and not s.get("active_special") and s.get("due_special"):
                    lines.append("موعد الجولة الخاصة "+when(s.get("due_special"))+" بتوقيت مصر.")
            if s.get("flood",0)>time.time():
                lines.append("فرض تيليجرام انتظارًا حتى "+when(s.get("flood"))+" بتوقيت مصر.")
            await self.show("\n".join(lines),[[btn("تحديث الحالة","status","refresh")],[BACK]])
        elif action.startswith(("groups:","blocked:","issues:")):
            kind,page = action.split(":")
            page = int(page)
            rows = []
            if kind=="groups":
                title = "المجموعات العامة · اختر مجموعة لمراجعة حذفها."
                items = [btn(t[:60],"del:"+str(i),"delete","danger") for i,k,t in s.groups()]
            elif kind=="blocked":
                title = "القائمة السوداء · اختر مجموعة للسماح بمحاولة الإرسال إليها مجددًا."
                items = [btn(k[:60],"restore:"+str(i),"blocked") for i,k in s.db.execute(
                    "SELECT rowid,target FROM blacklist WHERE account=? ORDER BY rowid",(s.account,))]
            else:
                title = "تفاصيل التعثر في أحدث جولة عامة وخاصة"
                items = []
                for key in ("general","special"):
                    r = s.get("active_"+key) or s.get("last_"+key) or {}
                    items.extend(r.get("issues",[]))
            batch = items[page*8:page*8+8]
            if kind=="issues":
                title += "\n"+"\n".join(batch)
            else:
                rows.extend([b] for b in batch)
            nav = []
            if page:
                nav.append(btn("السابق",f"{kind}:{page-1}","back"))
            if (page+1)*8<len(items):
                nav.append(btn("التالي",f"{kind}:{page+1}","resume"))
            await self.show(f"{title}\nالعدد الإجمالي {len(items)}",rows+([nav] if nav else [])+[[BACK]])
        elif action.startswith("del:"):
            i = int(action[4:])
            row = next((g for g in s.groups() if g[0]==i),None)
            if row:
                await self.confirm(f"هل تريد حذف «{row[2]}» من قائمة الإرسال؟","delete",i)
        elif action.startswith("restore:"):
            row = s.db.execute("SELECT target FROM blacklist WHERE account=? AND rowid=?", (s.account,int(action[8:]))).fetchone()
            if row:
                await self.confirm(f"هل تريد رفع «{row[0]}» من القائمة السوداء؟","restore",row[0])

    async def message(self, text):
        if text.startswith("/") or not self.input:
            return await self.screen("home")
        if self.input=="custom":
            if not re.fullmatch(r"\d{1,6}",text.strip()) or not 1<=int(text)<=525600:
                return await self.show("أرسل عددًا صحيحًا من ١ إلى ٥٢٥٦٠٠ دقيقة.",[[BACK]])
            return await self.screen("time:"+str(int(text)))
        inputs = list(dict.fromkeys(filter(None,re.split(r"[,،\s]+",text.strip()))))
        if len(inputs)>20:
            return await self.show("أرسل حتى ٢٠ مجموعة في الدفعة الواحدة.",[[BACK]])
        found,notes = await self.validate(inputs)
        self.input = None
        body = "\n".join(notes)
        if found:
            await self.confirm(f"هل تريد إضافة المجموعات التالية بدءًا من الجولة القادمة؟\n{body}", "add",found)
        else:
            await self.show("لم تُحدَّد مجموعات جديدة للإضافة.\n"+body,[[BACK]])

    def expire(self, message_id, seconds=0):
        with self.s.db:
            queue = self.s.get(self.cleanup_key, {})
            queue[str(message_id)] = time.time() + seconds
            self.s.set(self.cleanup_key, queue)

    async def cleanup(self):
        while True:
            for key, due in self.s.get(self.cleanup_key, {}).items():
                if due > time.time():
                    continue
                try:
                    await self.api("deleteMessage", chat_id=self.admin, message_id=int(key))
                except Exception as exc:
                    detail = str(exc).lower()
                    if not any(reason in detail for reason in ("message to delete not found", "message can't be deleted")):
                        self.expire(int(key), 60)
                        continue
                with self.s.db:
                    queue = self.s.get(self.cleanup_key, {})
                    queue.pop(key, None)
                    self.s.set(self.cleanup_key, queue)
            await asyncio.sleep(1)

    async def round_notice(self, text):
        sent = await self.api("sendMessage", chat_id=self.admin, text=text)
        self.expire(sent["message_id"], 60)

    async def run(self):
        offset = None
        while True:
            try:
                updates = await self.api("getUpdates",offset=offset,timeout=25,allowed_updates=["message","callback_query"])
                for u in updates:
                    offset = u["update_id"]+1
                    q = u.get("callback_query")
                    m = q.get("message",{}) if q else u.get("message",{})
                    if m.get("chat",{}).get("id")!=self.admin or m["chat"].get("type")!="private":
                        continue
                    if q:
                        if q["from"]["id"]!=self.admin:
                            continue
                        await self.api("answerCallbackQuery",callback_query_id=q["id"])
                        if m["message_id"]==self.mid:
                            await self.screen(q["data"])
                    elif m.get("text") and m.get("from",{}).get("id")==self.admin:
                        if not self.input and not m["text"].startswith("/"):
                            self.input = "add"
                        await self.message(m["text"])
                        self.expire(m["message_id"])
            except Exception as exc:
                logging.warning("Admin panel unavailable (%s)",type(exc).__name__)
                await asyncio.sleep(4)
