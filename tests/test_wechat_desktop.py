from io import BytesIO, StringIO
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw
from scripts.wechat_desktop import Desktop, bubble_direction, contact_search_matches, media_direction, quote_row_index


def bubble(outgoing=None):
    image = Image.new("RGB", (578, 57), (250, 250, 250))
    if outgoing is not None:
        draw = ImageDraw.Draw(image)
        draw.rectangle((300, 8, 508, 44) if outgoing else (50, 8, 230, 44),
                       fill=(157, 242, 159) if outgoing else (255, 255, 255))
    return image


class DesktopEvidenceTests(unittest.TestCase):
    def test_repeated_quote_text_uses_ordered_native_identity_and_neighbors(self):
        messages = [{'id':str(i),'text':text,'kind':'text'} for i,text in enumerate(
            ['same request','earlier answer','smaller file please','same request','same request'])]
        visible = ['smaller file please','same request','same request']
        self.assertEqual(quote_row_index(visible,messages,'3'),1)
        self.assertEqual(quote_row_index(visible,messages,'4'),2)
        self.assertIsNone(quote_row_index(visible,messages,'0'))
        # New chatter, including another copy of the request, does not shift the
        # uniquely anchored older target onto the newly arrived bubble.
        self.assertEqual(quote_row_index(visible+['same request'],messages,'4'),2)

    def test_quote_alignment_keeps_real_ambiguity_closed_and_handles_quoted_history(self):
        repeated=[{'id':str(i),'text':'same','kind':'text'} for i in range(3)]
        self.assertIsNone(quote_row_index(['same','same'],repeated,'2'))
        messages=[{'id':'reply','text':'answer','kind':'text','reply_to_server_id':'old'},
                  {'id':'target','text':'same','kind':'text'},
                  {'id':'next','text':'same','kind':'text'}]
        self.assertEqual(quote_row_index(["answer\nQuote User's message: old",'same','same'],messages,'target'),1)
        self.assertIsNone(quote_row_index(['different'],messages,'target'))

    def test_quote_locator_rejects_duplicate_bubbles(self):
        desktop=Desktop.__new__(Desktop)
        target={'id':'trigger','text':'@Me question','outgoing':False}
        row=SimpleNamespace(get_name=lambda:target['text'])
        desktop.chat_selected=lambda chat:True
        desktop.showing=lambda row:True
        desktop.message_list=lambda:SimpleNamespace(get_child_count=lambda:2,get_child_at_index=lambda i:row)
        with self.assertRaisesRegex(ValueError,'More than one'):
            desktop.find_quote_message({'name':'Group'},target)

    def test_quote_action_verifies_preview_without_typing_or_sending(self):
        desktop=Desktop.__new__(Desktop)
        row,menu=object(),object()
        desktop.quote_previews=Mock(return_value=[])
        desktop.find_quote_message=Mock(return_value=row)
        desktop.rect=lambda node:(0,0,578,80)
        desktop.click=Mock()
        desktop.named=Mock(return_value=[menu])
        desktop.quote_matches=Mock(return_value=True)
        desktop.begin_quote({'name':'Group'},{'text':'@Me question'})
        self.assertEqual(desktop.click.call_args_list[0].args,(row,))
        self.assertEqual(desktop.click.call_args_list[0].kwargs['button'],3)
        desktop.click.assert_called_with(menu)
    def test_native_prepare_skips_desktop_history_and_requires_commit(self):
        desktop = Desktop.__new__(Desktop)
        desktop.status = lambda:{'state':'ready','account_id':'account'}
        desktop.open_chat = Mock(return_value={'id':'chat','name':'Chat'})
        desktop.read_current = Mock(side_effect=AssertionError('Native preflight cannot read the desktop history'))
        desktop.send_current = Mock(return_value={'sent':True,'verified':False})
        with patch('scripts.wechat_desktop.sys.stdin',StringIO('{"commit":true,"text":"reply"}\n')), patch('builtins.print'):
            result = desktop.checked_send({'target':'chat','expected_account':'account','native_receipt':True})
        self.assertEqual(result,{'sent':True,'verified':False})
        desktop.read_current.assert_not_called()
        desktop.send_current.assert_called_once_with({'id':'chat','name':'Chat'},'reply',[],expected_account='account',native_receipt=True)
    def test_checked_send_prepare_does_not_send_without_commit(self):
        desktop = Desktop.__new__(Desktop)
        desktop.status = lambda:{'state':'ready','account_id':'account'}
        desktop.open_chat = Mock(return_value={'id':'chat','name':'Chat'})
        desktop.read_current = Mock(return_value=[{'id':'latest'}])
        desktop.send_current = Mock()
        with patch('scripts.wechat_desktop.sys.stdin',StringIO('{"commit":false}\n')), patch('builtins.print') as output:
            result=desktop.checked_send({'target':'chat','expected_account':'account'})
        self.assertEqual(result,{'cancelled':True})
        self.assertIn('latest',output.call_args.args[0])
        desktop.send_current.assert_not_called()

    def test_checked_send_commit_rechecks_account_before_touching_editor(self):
        desktop = Desktop.__new__(Desktop)
        desktop.status = Mock(side_effect=[{'state':'ready','account_id':'account'},
                                          {'state':'ready','account_id':'other'}])
        desktop.open_chat = Mock(return_value={'id':'chat','name':'Chat'})
        desktop.read_current = Mock(return_value=[])
        desktop.walk = Mock()
        with patch('scripts.wechat_desktop.sys.stdin',StringIO('{"commit":true,"text":"hello"}\n')), patch('builtins.print'):
            with self.assertRaisesRegex(ValueError,'account changed'):
                desktop.checked_send({'target':'chat','expected_account':'account'})
        desktop.walk.assert_not_called()

    def test_known_bubbles_and_unknown_theme_do_not_default_to_incoming(self):
        self.assertIs(bubble_direction(bubble(True)), True)
        self.assertIs(bubble_direction(bubble(False)), False)
        self.assertIsNone(bubble_direction(bubble()))
        self.assertIsNone(bubble_direction(Image.new("RGB", (578, 57), (30, 30, 30))))
        file_card = Image.new('RGB', (578, 100), (250, 250, 250))
        ImageDraw.Draw(file_card).rectangle((300, 8, 508, 92), fill=(255, 255, 255))
        self.assertIs(bubble_direction(file_card), True)

    def test_search_does_not_treat_internet_suggestions_as_recipients(self):
        def row(text):
            return SimpleNamespace(get_name=lambda: text)
        net, contact = row("Alice"), row("Alice")
        rows = [row("Internet search results"), net, row("Group Chats"), contact,
                row("Favorites"), row("Alice")]
        self.assertEqual(contact_search_matches(rows, "Alice"), [contact])

    def test_explicit_text_interface_avoids_accessible_method_collision(self):
        desktop = Desktop.__new__(Desktop)
        interface = SimpleNamespace(get_text=lambda: "deprecated getter")
        desktop.api = SimpleNamespace(Text=SimpleNamespace(get_text=Mock(return_value="真实文字")))
        node = SimpleNamespace(get_text_iface=lambda: interface)
        self.assertEqual(desktop.text(node), "真实文字")
        desktop.api.Text.get_text.assert_called_once_with(interface, 0, -1)

    def test_row_text_timestamp_and_self_direction_survive_sparse_qt_metadata(self):
        desktop = Desktop.__new__(Desktop)
        class Row:
            def __init__(self, text, y, h): self.text, self.y, self.h = text, y, h
            def get_name(self): return self.text
            def get_role_name(self): return "list item"
            def get_child_count(self): return 0
        clock, outgoing, incoming = Row("16:33", 0, 41), Row("Test Message Sent from Phone", 41, 57), Row("你好", 98, 57)
        rows = [clock, outgoing, incoming]
        container = SimpleNamespace(get_child_count=lambda: 3, get_child_at_index=lambda i: rows[i])
        desktop.message_list = lambda: container
        desktop.showing = lambda node: True
        desktop.walk = lambda node: iter([node])
        desktop.rect = lambda node: (0, 0, 578, 200) if node is container else (0, node.y, 578, node.h)
        desktop.identifier = lambda node: ""
        def capture(args, **kwargs):
            image = Image.new("RGB", (578, 200), (250, 250, 250))
            image.paste(bubble(True), (0, 41))
            image.paste(bubble(False), (0, 98))
            data = BytesIO(); image.save(data, "PNG")
            return SimpleNamespace(stdout=data.getvalue())
        with patch("scripts.wechat_desktop.subprocess.run", side_effect=capture) as screenshot:
            result = desktop.read_current({"id": "chat"}, 10)
        screenshot.assert_called_once()
        self.assertEqual([item["text"] for item in result], ["Test Message Sent from Phone", "你好"])
        self.assertTrue(result[0]["outgoing"])
        self.assertFalse(result[1]["outgoing"])
        self.assertEqual(result[1]["timestamp"], "16:33")
        self.assertNotEqual(result[0]["id"], result[1]["id"])

    def test_click_refuses_obscured_controls_before_button_press(self):
        desktop = Desktop.__new__(Desktop)
        desktop.app = SimpleNamespace(get_process_id=lambda: 123, clear_cache=lambda: None)
        desktop.showing = lambda node: True
        desktop.rect = lambda node: (10, 20, 50, 30)
        parent = SimpleNamespace(get_role_name=lambda: "filler")
        node = SimpleNamespace(get_parent=lambda: parent, get_role_name=lambda: "button")
        def run(args, **kwargs):
            if args[1] == "getmouselocation": return SimpleNamespace(stdout=b"WINDOW=42\n")
            if args[1] == "getwindowpid": return SimpleNamespace(returncode=0, stdout=b"999\n")
            return SimpleNamespace(returncode=0, stdout=b"")
        with patch("scripts.wechat_desktop.subprocess.run", side_effect=run) as subprocess:
            with self.assertRaisesRegex(ValueError, "obscured"):
                desktop.click(node)
        self.assertFalse(any(call.args[0][1] == "click" for call in subprocess.call_args_list))


    def test_repeated_reads_do_not_toggle_selected_chat_closed(self):
        desktop = Desktop.__new__(Desktop)
        desktop.app = SimpleNamespace(clear_cache=lambda: None)
        editor = SimpleNamespace(get_role_name=lambda: "text", get_name=lambda: "Alice")
        selected = []
        desktop.walk = lambda: iter(selected)
        desktop.resolve_row = lambda target: ("sidebar row", {"name": "Alice", "id": "name:Alice"})
        desktop.clear_search = Mock()
        desktop.chat_metadata = lambda chat: chat
        def click(row):
            selected[:] = [] if selected else [editor]
        desktop.click = Mock(side_effect=click)
        self.assertEqual(desktop.open_chat("Alice"), desktop.open_chat("Alice"))
        desktop.click.assert_called_once_with("sidebar row")
        self.assertTrue(desktop.chat_selected({"name": "Alice"}))

    def test_file_picker_opens_once_and_requires_a_new_outgoing_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'temp' / 'wechat_outbox' / 'job' / 'video.mp4'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'fixture')
            desktop = Desktop.__new__(Desktop)
            desktop.app = SimpleNamespace(clear_cache=lambda: None)
            desktop.status = lambda: {'state': 'ready'}
            desktop.open_chat = lambda target: {'name': 'Alice', 'id': 'Alice'}
            desktop.chat_selected = lambda chat: True
            field = SimpleNamespace(get_role_name=lambda: 'text', get_name=lambda: 'File name:',
                get_editable_text_iface=lambda: SimpleNamespace(set_text_contents=lambda value: value == str(path)))
            open_button = SimpleNamespace(get_role_name=lambda: 'button', get_name=lambda: 'Open')
            editor = SimpleNamespace()
            picker, button = object(), object()
            desktop.named = lambda names, role: [editor] if 'Alice' in names else ([picker] if 'Select File' in names else [button])
            desktop.walk = lambda dialog: iter([field, open_button])
            desktop.text = lambda node: str(path) if node is field else ''
            desktop.click = Mock()
            desktop.read_current = Mock(side_effect=[[], [{'id': 'receipt', 'outgoing': True, 'text': 'video.mp4\n12 KB'}]])
            with patch('scripts.wechat_desktop.__file__', str(root / 'scripts' / 'wechat_desktop.py')):
                result = desktop.send_file('Alice', str(path))
            self.assertEqual(result, {'verified': True, 'message_id': 'receipt'})
            self.assertEqual([call.args[0] for call in desktop.click.call_args_list], [button, open_button])

    def test_staged_file_needs_one_send_and_upload_progress_is_not_completion(self):
        desktop = Desktop.__new__(Desktop)
        desktop.app = SimpleNamespace(clear_cache=lambda: None)
        editor, send = object(), object()
        desktop.named = lambda names, role: [editor] if 'Alice' in names else [send]
        desktop.chat_selected = lambda chat: True
        desktop.text = lambda node: '\ufffc'
        desktop.click = Mock()
        desktop.read_current = Mock(side_effect=[
            [{'id':'video', 'outgoing':True, 'kind':'video', 'text':'Video Progress: 15%2:01'}],
            [{'id':'video', 'outgoing':True, 'kind':'video', 'text':'Video 2:01'}]])
        with patch('scripts.wechat_desktop.time.sleep'):
            receipt = desktop.finish_staged_file({'name':'Alice'}, Path('clip_1.mp4'), set())
        self.assertTrue(receipt['verified'])
        self.assertEqual(desktop.read_current.call_count, 2)
        desktop.click.assert_called_once_with(send)

    def test_media_direction_requires_one_sided_card_evidence(self):
        right = Image.new('RGB',(578,159),(250,250,250))
        ImageDraw.Draw(right).rectangle((259,8,505,145), fill=(45,72,120))
        self.assertIs(media_direction(right),True)
        left = Image.new('RGB',(578,159),(250,250,250))
        ImageDraw.Draw(left).rectangle((73,8,320,145), fill=(45,72,120))
        self.assertIs(media_direction(left),False)
        ImageDraw.Draw(right).rectangle((60,8,245,145), fill=(45,72,120))
        self.assertIsNone(media_direction(right))

    def test_native_file_send_releases_desktop_after_one_send(self):
        desktop = Desktop.__new__(Desktop)
        desktop.app = SimpleNamespace(clear_cache=lambda: None)
        editor, send = object(), object()
        desktop.named = lambda names, role: [editor] if 'Alice' in names else [send]
        desktop.chat_selected = lambda chat: True
        desktop.text = lambda node: '\ufffc'
        desktop.click = Mock()
        desktop.read_current = Mock()
        result = desktop.finish_staged_file({'name':'Alice'}, Path('clip.mp4'), set(), native_receipt=True)
        self.assertEqual(result, {'sent':True,'verified':False})
        desktop.click.assert_called_once_with(send)
        desktop.read_current.assert_not_called()
