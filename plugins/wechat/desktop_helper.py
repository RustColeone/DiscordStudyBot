"""One bounded JSON operation against Linux WeChat's accessibility controls.

Run with /usr/bin/python3. Requests arrive on stdin; stdout contains only JSON.
Copied from scripts/wechat_desktop.py; the parent sets WECHAT_OUTBOX to its private outbox.
Selectors are centralized here so WeChat UI changes cannot silently redirect sends.
"""
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


def bubble_direction(image):
    """Direction evidence for the client's light text bubbles; unknown themes fail closed.

    Linux Qt exposes message text but omits child/avatar metadata. Do not infer an
    incoming message merely from the absence of a green bubble.
    """
    from collections import Counter
    image = image.convert("RGB")
    width, height = image.size
    if width < 160 or height < 20:
        return None
    background = image.getpixel((0, 0))
    if not all(246 <= channel <= 253 for channel in background):
        return None
    right = Counter(image.crop((width - 140, 5, width - 45, height - 5)).getdata())
    green = sum(count for (r, g, b), count in right.items() if g > r + 50 and g > b + 50 and g > 180)
    if green >= 100:
        return True
    left = Counter(image.crop((45, 5, 140, height - 5)).getdata())
    white = left.get((255, 255, 255), 0)
    gray = sum(count for (r, g, b), count in left.items()
               if max(r, g, b) - min(r, g, b) <= 2 and 215 <= r <= 240)
    right_white = right.get((255, 255, 255), 0)
    if right_white >= 150 and white + gray < 150:
        return True
    return False if white + gray >= 150 else None


def contact_search_matches(rows, target):
    """Ignore Favorites/history/internet suggestions even when their title matches."""
    categories = {"Contacts", "Group Chats", "Chats", "联系人", "群聊", "会话"}
    boundaries = categories | {"Internet search results", "Favorites", "Chat History", "Search Chat History",
                               "网络搜索", "收藏", "聊天记录"}
    in_contacts = False
    matches = []
    for row in rows:
        label = row.get_name().strip()
        if label in boundaries:
            in_contacts = label in categories
        elif in_contacts and target in label.splitlines():
            matches.append(row)
    return matches


def media_direction(image):
    """Known media cards occupy one side; avatars are outside these regions."""
    image = image.convert('RGB')
    width, height = image.size
    if width < 160 or height < 20 or not all(246 <= c <= 253 for c in image.getpixel((0, 0))):
        return None
    background = image.getpixel((0, 0))
    def occupied(box):
        return sum(1 for color in image.crop(box).getdata() if max(abs(a-b) for a,b in zip(color, background)) > 12)
    left = occupied((45, 5, width//2-20, height-5))
    right = occupied((width//2+20, 5, width-45, height-5))
    threshold = max(500, (width//2 - 65) * (height-10) // 4)
    if right >= threshold and right > left * 8:
        return True
    if left >= threshold and left > right * 8:
        return False
    return None


def quote_row_index(names, messages, target_id):
    """Align visible rows with native message order, including repeated text."""
    targets = [i for i, item in enumerate(messages) if item.get('id') == target_id]
    if len(targets) != 1:
        return None
    target = targets[0]

    def matches(name, item):
        text = str(item.get('text') or '').strip()
        if item.get('kind', 'text') == 'text':
            return name == text or bool(item.get('reply_to_server_id') and
                                       name.startswith(text + '\nQuote '))
        patterns = {'video':r'Video(?: Progress: \d+%)?\s*\d+:\d{2}',
                    'image':r'(?:Photo|Image|图片|照片)(?: Progress: \d+%)?',
                    'sticker':r'(?:Animated Stickers|Sticker|动画表情)'}
        return bool(re.fullmatch(patterns.get(item.get('kind'), r'(?!)'), name))

    candidates = []
    for index, name in enumerate(names):
        if not matches(name, messages[target]):
            continue
        left = right = index
        while left > 0 and target-(index-left)-1 >= 0 and matches(names[left-1], messages[target-(index-left)-1]):
            left -= 1
        while right+1 < len(names) and target+(right-index)+1 < len(messages) and matches(names[right+1], messages[target+(right-index)+1]):
            right += 1
        # A repeated bubble needs an unambiguous surrounding sequence. A unique
        # body can still be located without requiring unrelated neighboring rows.
        block = names[left:right+1]
        starts = [start for start in range(len(messages)-len(block)+1)
                  if all(matches(raw, messages[start+offset]) for offset, raw in enumerate(block))]
        if starts == [target-(index-left)]:
            candidates.append((len(block), index))
    if not candidates:
        return None
    longest = max(length for length, _ in candidates)
    strongest = [index for length, index in candidates if length == longest]
    return strongest[0] if len(strongest) == 1 else None


class Desktop:
    def __init__(self):
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
        self.api = Atspi
        Atspi.set_timeout(1000, 2000)
        desktop = Atspi.get_desktop(0)
        apps = [desktop.get_child_at_index(i) for i in range(desktop.get_child_count())]
        self.app = next((app for app in apps if app.get_name().lower() in {"wechat", "weixin", "微信"}), None)
        if self.app is None:
            raise ValueError("WeChat is not running with accessibility enabled. Use --login to open it.")

    def showing(self, node):
        return node.get_state_set().contains(self.api.StateType.SHOWING)

    def walk(self, root=None):
        stack = [root or self.app]
        visited = 0
        while stack and visited < 6000:
            node = stack.pop()
            visited += 1
            yield node
            for i in reversed(range(min(node.get_child_count(), 500))):
                child = node.get_child_at_index(i)
                if child is not None and self.showing(child):
                    stack.append(child)

    @staticmethod
    def identifier(node):
        return node.get_accessible_id() or ""

    def text(self, node):
        interface = node.get_text_iface()
        # GI returns Accessible for the interface; its deprecated get_text()
        # getter shadows Text.get_text(start,end), so dispatch explicitly.
        return self.api.Text.get_text(interface, 0, -1).strip() if interface is not None else node.get_name().strip()

    def rect(self, node):
        r = node.get_component_iface().get_extents(self.api.CoordType.SCREEN)
        return r.x, r.y, r.width, r.height

    def click(self, node, button=1, offset=None):
        if not self.showing(node):
            raise ValueError("The selected WeChat control is no longer visible.")
        x, y, width, height = self.rect(node)
        parent = node.get_parent()
        visible = None
        if node.get_role_name() == "list item" and parent.get_role_name() == "list":
            px, py, pw, ph = self.rect(parent)
            for _ in range(12):
                x, y, width, height = self.rect(node)
                visible = (max(y, py), min(y + height, py + ph))
                if py <= y and y + height <= py + ph:
                    break
                # A long message bubble can be taller than the list viewport and never fit;
                # a clear visible slice of it is enough to click inside the same item.
                if height > ph and visible[1] - visible[0] >= min(ph // 2, 80):
                    break
                if height > ph:
                    direction = "5" if y > py else "4"
                else:
                    direction = "5" if y + height > py + ph else "4"
                subprocess.run(["xdotool", "mousemove", str(px + pw // 2), str(py + ph // 2),
                                "click", "--repeat", "3", "--delay", "30", direction],
                               capture_output=True, check=True, timeout=3)
                time.sleep(0.1)
                self.app.clear_cache()
                node.clear_cache()
            else:
                raise ValueError("The recipient could not be scrolled into view; no click attempted.")
        if width <= 0 or height <= 0:
            raise ValueError("The selected WeChat control has no activation area.")
        # Qt's exposed Action methods can report success without activating the
        # custom widget. X11 coordinates track the actual compositor placement.
        dx,dy = offset or (width//2,height//2)
        if visible is not None:
            dy = min(max(y + dy, visible[0] + 10), visible[1] - 10) - y
        self.move_owned(x+dx,y+dy)
        subprocess.run(["xdotool", "click", str(button)], capture_output=True, check=True, timeout=3)
        time.sleep(0.3)
        self.app.clear_cache()

    def move_owned(self, x, y):
        subprocess.run(["xdotool", "mousemove", str(x), str(y)],
                       capture_output=True, check=True, timeout=3)
        location = subprocess.run(["xdotool", "getmouselocation", "--shell"], capture_output=True, check=True, timeout=3).stdout.decode()
        window = next((line.split("=", 1)[1] for line in location.splitlines() if line.startswith("WINDOW=")), "0")
        owner = subprocess.run(["xdotool", "getwindowpid", window], capture_output=True, timeout=3)
        if owner.returncode or owner.stdout.decode().strip() != str(self.app.get_process_id()):
            raise ValueError("The selected WeChat control is obscured by another window; no click attempted.")

    def quote_previews(self, chat):
        editors = self.named({chat['name']},'text')
        sends = self.named({'Send','发送'},'button')
        if len(editors)!=1 or len(sends)!=1:
            return []
        ex,ey,ew,eh = self.rect(editors[0]);sy = self.rect(sends[0])[1]
        return [node for node in self.walk() if node.get_role_name() in {'label','invalid'}
                and node.get_component_iface() is not None and node.get_name().strip()
                and ex <= self.rect(node)[0] < ex+ew and ey+eh <= self.rect(node)[1] < sy]

    def find_quote_message(self, chat, target):
        if (not isinstance(target,dict) or not isinstance(target.get('text'),str)
                or not target['text'].strip() or target.get('outgoing') is not False):
            raise ValueError('A quoted reply requires one exact incoming trigger')
        for attempt in range(13):
            if not self.chat_selected(chat):
                raise ValueError('The quote recipient changed; no send attempted')
            container = self.message_list()
            rows = [container.get_child_at_index(i) for i in range(container.get_child_count())]
            context = target.get('ui_context')
            if context:
                visible = [row for row in rows if row is not None and self.showing(row) and row.get_name().strip()
                           and not (self.rect(row)[3] <= 45 and re.fullmatch(
                               r'(?:.*\s)?\d{1,2}:\d{2}(?:\s?[AP]M)?',row.get_name().strip(),re.I))]
                index = quote_row_index([row.get_name().strip() for row in visible], context, target['id'])
                if index is not None:
                    return visible[index]
                matches = []
            else:
                matches = [row for row in rows if row is not None and self.showing(row)
                           and row.get_name().strip()==target['text'].strip()]
            if len(matches)>1:
                raise ValueError('More than one visible message matches the quote; no send attempted')
            if len(matches)==1:
                return matches[0]
            x,y,w,h = self.rect(container)
            self.move_owned(x+w//2,y+h//2)
            repeats,delay,direction = ('60','5','5') if attempt==0 else ('4','60','4')
            subprocess.run(['xdotool','click','--repeat',repeats,'--delay',delay,direction],
                           check=True,capture_output=True,timeout=3)
            time.sleep(.15)
            self.app.clear_cache()
        raise ValueError('The exact quote target could not be found in the bounded chat view')

    def begin_quote(self, chat, target):
        if self.quote_previews(chat):
            raise ValueError('This chat already has a quote draft; no send attempted')
        row = self.find_quote_message(chat,target)
        x,y,w,h = self.rect(row)
        # Sparse Qt text rows span the full list width. Incoming bubbles begin
        # after the avatar, so activate the text bubble rather than empty space.
        self.click(row,button=3,offset=(min(90,w//2),h//2))
        menus = self.named({'Quote','Reply','引用','回复'},'menu item')
        if len(menus)!=1:
            editors = self.named({chat['name']},'text')
            if len(editors)==1:
                self.click(editors[0])
            raise ValueError('The native quote action is unavailable; no send attempted')
        self.click(menus[0])
        if not self.quote_matches(chat,target):
            self.cancel_quote(chat)
            raise ValueError('The quoted preview did not match the triggering message')

    def cancel_quote(self, chat):
        buttons = self.named({'Delete quote','Delete Quote','Remove quote','取消引用','删除引用'},'button')
        if len(buttons)!=1:
            raise ValueError('The quote cleanup control is unavailable; the draft was left untouched')
        self.click(buttons[0])
        if self.quote_previews(chat):
            raise ValueError('The quote draft could not be cleared')

    def quote_matches(self, chat, target):
        prefix = target['text'].strip()[:12]
        return bool(prefix) and any(prefix in node.get_name() for node in self.quote_previews(chat))

    def named(self, names, role=None):
        return [n for n in self.walk() if n.get_name() in names and (role is None or n.get_role_name() == role)]

    def status(self):
        nodes = list(self.walk())
        names = {n.get_name() for n in nodes}
        main = any(n.get_role_name() == "list" and n.get_name() in {"Chats", "聊天", "会话"} for n in nodes)
        login = {"Scan to log in", "Scan QR code to log in", "扫码登录", "扫描二维码登录", "Scan again"}
        if not main and names & login:
            return {"state": "logged_out", "account_id": "", "name": "", "detail": "Scan the login QR and confirm on your phone."}
        if not main and names & {"Log in", "Log In", "Login", "Confirm login", "登录", "确认登录", "Enter Weixin", "进入微信"}:
            return {"state": "confirming", "account_id": "", "name": "", "detail": "Login needs confirmation."}
        sessions = self.session_rows(nodes)
        if not sessions and not names & {"Chats", "Contacts", "聊天", "通讯录"}:
            return {"state": "unavailable", "account_id": "", "name": "", "detail": "The current WeChat screen is not a recognized account/inbox view."}
        profiles = [n for n in nodes if self.identifier(n).lower() in {"avatar", "self_avatar", "profile", "user_avatar"}]
        # The UI exposes an avatar button with the account name on this client.
        if not profiles:
            profiles = [n for n in nodes if n.get_role_name() == "button" and
                        any(x in self.identifier(n).lower() for x in ("avatar", "profile", "account"))]
        name = profiles[0].get_name().strip() if profiles else ""
        account_id = "desktop:" + hashlib.sha256(name.encode()).hexdigest()[:16] if name else ""
        if not account_id:
            # The Linux build may omit the own-avatar accessible name. A single
            # account directory is an unambiguous local identity; never select
            # the newest directory when multiple accounts have used this client.
            directory = Path.home() / "Documents" / "xwechat_files"
            accounts = [p.name for p in directory.iterdir() if p.is_dir() and p.name.startswith("wxid_")] if directory.exists() else []
            if len(accounts) == 1:
                account_id = accounts[0]
                name = accounts[0].rsplit("_", 1)[0]
        return {"state": "ready" if account_id else "identity_unknown", "account_id": account_id,
                "name": name, "detail": "" if name else "Account identity is not exposed by the current UI; automatic replies are paused."}

    def session_rows(self, nodes=None):
        nodes = list(self.walk()) if nodes is None else nodes
        rows = []
        for node in nodes:
            identifier = self.identifier(node)
            if identifier.startswith("session_item_"):
                rows.append(node)
        if rows:
            return rows
        lists = [n for n in nodes if self.identifier(n) == "session_list"]
        if not lists:
            lists = [n for n in nodes if n.get_role_name() == "list" and n.get_name() in {"Chats", "聊天", "会话"}]
        if len(lists) != 1:
            return []
        return [lists[0].get_child_at_index(i) for i in range(lists[0].get_child_count())
                if self.showing(lists[0].get_child_at_index(i))]

    def chat(self, row):
        label = row.get_name().strip()
        identifier = self.identifier(row)
        children = [n for n in self.walk(row) if n is not row and n.get_role_name() == "label" and n.get_name().strip()]
        name = children[0].get_name().strip() if children else label.splitlines()[0]
        unread = re.search(r"(?:\[(\d+)(?:条)?\]|(\d+) unread|(\d+)条新消息)", label, re.I)
        lines = [line for line in label.splitlines()[1:] if line.strip() and not re.fullmatch(r"\[\d+\]|\d{1,2}:\d{2}", line)
                 and line not in {"Pinned to top", "Mute Notifications"}]
        return {"id": identifier or "name:" + name, "name": name,
                "preview": lines[0] if lines else "",
                "unread": int(next(v for v in unread.groups() if v)) if unread else 0}

    def chats(self):
        if self.status()["state"] not in {"ready", "identity_unknown"}:
            raise ValueError("WeChat is not logged in. Use --login first.")
        return [self.chat(row) for row in self.session_rows()]

    def resolve_row(self, target):
        matches = [(row, self.chat(row)) for row in self.session_rows()
                   if target in {self.chat(row)["id"], self.chat(row)["name"]}]
        if not matches:
            query = target[5:] if target.startswith("name:") else target
            row = self.search_contact(query)
            if row is not None:
                return row, {"id": "name:" + query, "name": query, "unread": 0}
        if len(matches) != 1:
            raise ValueError("Recipient was not found uniquely in the visible chat list. Use --chats and an exact ID; open the chat in WeChat if it is not listed.")
        return matches[0]

    def search_contact(self, target):
        fields = self.named({"Search", "搜索"}, "text")
        if len(fields) != 1 or self.text(fields[0]):
            raise ValueError("WeChat's search field is busy or unavailable. Clear it before selecting a recipient.")
        self.click(fields[0])
        if not fields[0].get_editable_text_iface().set_text_contents(target):
            raise ValueError('The search field did not accept the recipient name')
        # Native editing signals trigger search; direct AT-SPI setText alone does
        # not. Avoid XKB Unicode remapping, which can corrupt longer CJK names.
        subprocess.run(['xdotool','key','--clearmodifiers','End','space','BackSpace'],
                       check=True,capture_output=True,timeout=3)
        for _ in range(12):
            time.sleep(0.15)
            self.app.clear_cache()
            if self.text(fields[0]) != target:
                continue
            lists = [n for n in self.walk() if n.get_role_name() == "list" and not n.get_name()]
            matches = []
            for container in lists:
                rows = [container.get_child_at_index(i) for i in range(container.get_child_count())]
                matches.extend(contact_search_matches(rows, target))
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                break
        self.clear_search()
        return None

    def clear_search(self):
        fields = self.named({"Search", "搜索"}, "text")
        if len(fields) == 1 and self.text(fields[0]):
            self.click(fields[0])
            subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+a", "BackSpace", "Escape"],
                           capture_output=True, check=True, timeout=3)
            self.app.clear_cache()

    def open_chat(self, target):
        if self.named({'Members'},'list'):
            buttons=self.named({'Chat Info'},'button')
            if len(buttons)!=1:
                raise ValueError('A group information pane obscures the messages; close it first')
            self.click(buttons[0])
        row, chat = self.resolve_row(target)
        self.app.clear_cache()
        # This Linux client toggles an already selected sidebar chat closed.
        # Repeated reads must keep the verified conversation open.
        if self.chat_selected(chat):
            self.clear_search()
            return self.chat_metadata(chat)
        self.click(row)
        self.clear_search()
        for _ in range(8):
            self.app.clear_cache()
            if self.chat_selected(chat):
                return self.chat_metadata(chat)
            time.sleep(0.1)
        else:
            raise ValueError("The selected conversation header/editor did not match the recipient; no send attempted.")

    def chat_selected(self, chat):
        editors = [n for n in self.walk() if n.get_role_name() == "text" and n.get_name() == chat["name"]]
        return len(editors) == 1

    def chat_metadata(self, chat):
        container=self.message_list()
        cx,cy,_,_=self.rect(container)
        counts=[]
        for n in self.walk():
            if n.get_role_name()=='label':
                x,y,w,h=self.rect(n)
                if x>=cx and y+h<=cy and re.search(r'[（(]\d+[）)]',n.get_name()):
                    counts.append(n.get_name())
        return {**chat,'chat_kind':'group' if counts else 'unknown'}

    def message_list(self):
        lists = [n for n in self.walk() if self.identifier(n) in {"message_list", "chat_message_list", "messages"}
                 or (n.get_role_name() == "list" and n.get_name() in {"Messages", "消息", "聊天记录"})]
        if len(lists) != 1:
            raise ValueError("The current client did not expose one unambiguous message list; no send was attempted.")
        return lists[0]

    def read_current(self, chat, limit):
        container = self.message_list()
        result, occurrences = [], {}
        count = container.get_child_count()
        timestamp = ""
        captured = None
        cx, cy, cw, ch = self.rect(container)
        for i in range(max(0, count - 250), count):
            row = container.get_child_at_index(i)
            if row is None:
                continue
            raw_name = row.get_name()
            raw = raw_name.strip()
            # Keep time anchors even when their rows scroll outside the view.
            # Otherwise old messages acquire new IDs as the viewport moves.
            if self.rect(row)[3] <= 45 and re.fullmatch(r"(?:.*\s)?\d{1,2}:\d{2}(?:\s?[AP]M)?", raw, re.I):
                timestamp = raw
                continue
            if not self.showing(row):
                continue
            nodes = list(self.walk(row))
            texts = [self.text(n) for n in nodes if n.get_role_name() in {"text", "label", "paragraph"} and self.text(n)]
            avatars = [n for n in nodes if n.get_role_name() in {"button", "image"} and n.get_name().strip()]
            if not texts and not raw:
                continue
            content = texts[-1] if texts else raw
            video = bool(re.fullmatch(r'Video(?: Progress: \d+%)?\s*\d+:\d{2}', raw))
            photo = bool(re.fullmatch(r'(?:Photo|Image|图片|照片)(?: Progress: \d+%)?', raw))
            sender = avatars[0].get_name().strip() if avatars else ""
            outgoing = None
            if avatars:
                x, _, width, _ = self.rect(container)
                ax, _, aw, _ = self.rect(avatars[0])
                outgoing = ax + aw / 2 > x + width / 2
            elif row.get_role_name() == "list item" and row.get_child_count() == 0:
                from PIL import Image
                x, y, width, height = self.rect(row)
                x1, y1, x2, y2 = max(x, cx), max(y, cy), min(x + width, cx + cw), min(y + height, cy + ch)
                if x2 - x1 == width and y2 - y1 == height and (photo or content not in {
                    "Animated Stickers", "Sticker", "Photo", "Image", "Video", "Voice", "动画表情", "图片", "视频", "语音"}):
                    if captured is None:
                        screenshot = subprocess.run(["grim", "-g", f"{cx},{cy} {cw}x{ch}", "-"],
                                                    check=True, capture_output=True, timeout=5).stdout
                        captured = Image.open(BytesIO(screenshot)).convert("RGB")
                    crop = captured.crop((x1-cx, y1-cy, x2-cx, y2-cy))
                    outgoing = media_direction(crop) if video or photo else bubble_direction(crop)
            kind = 'video' if video else ('image' if photo else ("text" if outgoing is not None else "unsupported"))
            # Rendering/visibility can change direction confidence; that must
            # not turn an old observed row into a new incoming message.
            stable_content = re.sub(r'^Video(?: Progress: \d+%)?\s*', 'Video ', content) if video else content
            if photo:
                stable_content = 'Image'
            stable_content = re.sub(r'\s+', ' ', stable_content).strip() if video else stable_content
            fingerprint = json.dumps([chat["id"], sender, stable_content, timestamp], ensure_ascii=False)
            occurrence = occurrences.get(fingerprint, 0)
            occurrences[fingerprint] = occurrence + 1
            identifier = self.identifier(row) or "ui:" + hashlib.sha256((fingerprint + str(occurrence)).encode()).hexdigest()
            result.append({"id": identifier, "chat_id": chat["id"], "sender_id": sender, "sender_name": sender,
                           "text": content, "outgoing": outgoing, "kind": kind, "timestamp": timestamp,
                           'chat_kind':chat.get('chat_kind','unknown'),
                           'mention_names':list(dict.fromkeys(re.findall(r'@([^@\r\n\u2005]{1,128})\u2005',raw_name+'\n'+content))) or None,
                           "content_key": hashlib.sha256(json.dumps([sender, stable_content], ensure_ascii=False).encode()).hexdigest()})
        return result[-max(1, min(int(limit), 100)):]

    def messages(self, target, limit=20):
        chat = self.open_chat(target)
        return self.read_current(chat, limit)

    def send(self, target, text):
        if self.status()["state"] != "ready":
            raise ValueError("WeChat account identity is not verified; no message sent.")
        chat = self.open_chat(target)
        before = self.read_current(chat, 100)
        return self.send_current(chat, text, before)

    def send_current(self, chat, text, before, expected_account=None, native_receipt=False, reply_to=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('A WeChat message must contain 1 to 4000 characters')
        if expected_account and self.status().get('account_id') != expected_account:
            raise ValueError('The account changed before commit; no message sent')
        if not self.chat_selected(chat):
            raise ValueError('The recipient changed before commit; no message sent')
        editors = [n for n in self.walk() if n.get_role_name() in {"text", "entry", "paragraph"}
                   and n.get_editable_text_iface() is not None and n.get_state_set().contains(self.api.StateType.EDITABLE)]
        editors = [n for n in editors if self.identifier(n) not in {"search", "search_input"} and n.get_name() not in {"Search", "搜索"}]
        buttons = self.named({"Send", "发送"}, "button")
        if len(editors) != 1 or len(buttons) != 1:
            raise ValueError("The current chat did not expose one message editor and Send button; no message sent.")
        if self.text(editors[0]):
            raise ValueError("This chat has an unsent draft. Clear or send it in WeChat before using the bridge.")
        if self.quote_previews(chat):
            raise ValueError('This chat has an existing quote draft; no send attempted')
        if reply_to:
            self.begin_quote(chat,reply_to)
        if not editors[0].get_editable_text_iface().set_text_contents(text):
            raise ValueError("WeChat did not accept the message draft; no send attempted.")
        if self.text(editors[0]) != text:
            raise ValueError("Message draft could not be verified; no send attempted.")
        if editors[0].get_name() != chat["name"]:
            raise ValueError("The selected recipient changed while composing; no send attempted.")
        if reply_to and not self.quote_matches(chat,reply_to):
            raise ValueError('The quote target changed before Send; no send attempted')
        self.click(buttons[0])
        if native_receipt:
            # The parent verifies one newly committed outgoing database row.
            # This means only "Send was clicked", never a successful receipt.
            return {'sent': True, 'verified': False}
        previous = {m["id"] for m in before}
        for _ in range(12):
            after = self.read_current(chat, 100)
            matches = [m for m in after if m["id"] not in previous and m["outgoing"] and m["text"] == text]
            if matches and not self.text(editors[0]):
                return {"verified": True, "message_id": matches[-1]["id"]}
            time.sleep(0.2)
        return {"verified": False}

    def checked_send(self, request):
        account = self.status()
        expected = request.get('expected_account')
        if account.get('state') == 'ready' and account.get('account_id') == expected:
            chat = self.open_chat(request['target'])
            before = [] if request.get('native_receipt') else self.read_current(chat, 100)
        else:
            chat, before = None, []
        # Two-phase private stdin/stdout protocol. No draft or send is attempted
        # until the controller has checked these latest messages and commits.
        print(json.dumps({'ok': True, 'result': {'account': account, 'messages': before}}, ensure_ascii=False), flush=True)
        commit = json.loads(sys.stdin.readline())
        if not commit.get('commit') or chat is None:
            return {'cancelled': True}
        reply_to = commit.get('reply_to') or request.get('reply_to')
        options = {'reply_to':reply_to} if reply_to else {}
        return self.send_current(chat, commit.get('text'), before, expected_account=expected,
                                 native_receipt=bool(request.get('native_receipt')), **options)

    def send_file(self, target, path, native_receipt=False):
        source = Path(path).resolve()
        outbox = Path(os.environ.get('WECHAT_OUTBOX') or Path(__file__).resolve().parent / 'outbox').resolve()
        if not source.is_relative_to(outbox) or not source.is_file() or source.stat().st_size == 0:
            raise ValueError('Only prepared private outbox files can be sent')
        if self.status()['state'] != 'ready':
            raise ValueError('WeChat account identity is not verified; no file sent')
        chat = self.open_chat(target)
        before = set() if native_receipt else {m['id'] for m in self.read_current(chat, 100)}
        editors = self.named({chat['name']}, 'text')
        if len(editors) != 1 or self.text(editors[0]):
            raise ValueError('This chat has an unsent draft or ambiguous editor; no file sent')
        buttons = self.named({'Send File', '发送文件'}, 'button')
        if len(buttons) != 1:
            raise ValueError('The client did not expose one file button; no file sent')
        self.click(buttons[0])
        dialogs = self.named({'Select File', '选择文件'}, 'dialog')
        if len(dialogs) != 1:
            raise ValueError('The native file picker was not identified; no file chosen')
        nodes = list(self.walk(dialogs[0]))
        fields = [n for n in nodes if n.get_role_name() == 'text' and n.get_name() in {'File name:', '文件名:'}]
        opens = [n for n in nodes if n.get_role_name() == 'button' and n.get_name() in {'Open', '打开'}]
        if len(fields) != 1 or len(opens) != 1:
            raise ValueError('The file picker is ambiguous; no file chosen')
        if not fields[0].get_editable_text_iface().set_text_contents(str(source)) or self.text(fields[0]) != str(source):
            raise ValueError('The file path was not accepted; no file chosen')
        if not self.chat_selected(chat):
            raise ValueError('The recipient changed before file selection; no file chosen')
        self.click(opens[0])
        return self.finish_staged_file(chat, source, before, native_receipt=native_receipt)

    def finish_staged_file(self, chat, source, before, native_receipt=False):
        # On this client Open stages one embedded object in the empty editor.
        # Some clients send immediately instead; only click Send for a verified
        # single newly staged object, and never repeat Open or Send.
        self.app.clear_cache()
        editors = self.named({chat['name']}, 'text')
        if len(editors) != 1 or not self.chat_selected(chat):
            return {'verified': False}
        draft = self.text(editors[0])
        if draft == '\ufffc':
            sends = self.named({'Send', '发送'}, 'button')
            if len(sends) != 1:
                return {'verified': False}
            self.click(sends[0])
        elif draft:
            return {'verified': False}
        if native_receipt:
            # The parent checks the committed database row. Release the desktop
            # while the upload runs; clicking Send alone is not delivery proof.
            return {'sent': True, 'verified': False}
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            self.app.clear_cache()
            if not self.chat_selected(chat):
                return {'verified': False}
            matches = [m for m in self.read_current(chat, 100) if m['id'] not in before
                       and m['outgoing'] is True and 'Progress:' not in m['text']
                       and (source.name in m['text'].splitlines() or
                            (m['kind'] == 'video' and source.suffix.lower() in {'.mp4', '.mov', '.m4v', '.webm'}) or
                            (m['kind'] == 'image' and source.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'}))]
            if matches:
                return {'verified': True, 'message_id': matches[-1]['id']}
            time.sleep(0.5)
        return {'verified': False}

    def login(self, output_dir):
        state = self.status()
        if state["state"] in {"ready", "identity_unknown"}:
            return {**state, "path": None}
        confirms = self.named({"Log in", "Log In", "Login", "Confirm login", "Enter Weixin", "登录", "进入微信", "确认登录"}, "button")
        if len(confirms) == 1:
            self.click(confirms[0])
            return {**self.status(), "path": None}
        buttons = self.named({"QR Code", "Scan again", "Refresh", "重新获取二维码", "刷新二维码"}, "button")
        if buttons:
            self.click(buttons[0])
        frames = [n for n in self.walk() if n.get_role_name() in {"frame", "window"}]
        if not frames:
            raise ValueError("WeChat login window is not visible.")
        x, y, w, h = self.rect(frames[0])
        if w <= 0 or h <= 0:
            raise ValueError("WeChat login window has no capture area.")
        path = Path(output_dir) / ("wechat-login-" + uuid.uuid4().hex + ".png")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        subprocess.run(["grim", "-g", f"{x},{y} {w}x{h}", str(path)], check=True, capture_output=True, timeout=5)
        path.chmod(0o600)
        return {**state, "path": str(path)}

    def confirm_login(self):
        confirms = self.named({"Log in", "Log In", "Login", "Confirm login", "Enter Weixin", "登录", "进入微信", "确认登录"}, "button")
        if len(confirms) == 1:
            self.click(confirms[0])
        return self.status()

    def diagnose(self):
        return [{"role": n.get_role_name(), "name": n.get_name(), "id": self.identifier(n),
                 "children": n.get_child_count(), "rect": self.rect(n) if n.get_component_iface() else None}
                for n in self.walk()]


def execute(request):
    try:
        desktop = Desktop()
    except ValueError:
        if request["operation"] != "login":
            raise
        subprocess.Popen(["/opt/wechat/wechat"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(20):
            time.sleep(0.25)
            try:
                desktop = Desktop()
                break
            except ValueError:
                pass
        else:
            raise ValueError("WeChat did not expose a login window. Check the desktop session and accessibility setting.")
    operation = request["operation"]
    if operation in {'send','send_file'} and request.get('expected_account'):
        if desktop.status().get('account_id')!=request['expected_account']:
            raise ValueError('The selected account changed; no message sent')
    if operation == "status":
        return desktop.status()
    if operation == "login":
        return desktop.login(request["output_dir"])
    if operation == "confirm_login":
        return desktop.confirm_login()
    if operation == "chats":
        return desktop.chats()
    if operation == "resolve":
        _, result = desktop.resolve_row(request["target"])
        desktop.clear_search()
        return result
    if operation == "messages":
        return desktop.messages(request["target"], request.get("limit", 20))
    if operation == "checked_send":
        return desktop.checked_send(request)
    if operation == "send":
        return desktop.send(request["target"], request["text"])
    if operation == 'send_file':
        return desktop.send_file(request['target'], request['path'], native_receipt=request.get('native_receipt', False))
    if operation == "diagnose":
        return desktop.diagnose()
    raise ValueError("Unknown desktop operation")


if __name__ == "__main__":
    try:
        request = json.loads(sys.stdin.readline())
        result = execute(request)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except Exception as error:
        detail = str(error) if isinstance(error, ValueError) else f"Desktop access failed ({type(error).__name__}); check the WeChat desktop session."
        print(json.dumps({"ok": False, "error": detail}, ensure_ascii=False))
