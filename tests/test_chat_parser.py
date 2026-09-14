import unittest

from parsers.command_parsers import parse_chat_command


class ChatParserTests(unittest.TestCase):
    def test_parses_chat_mode(self):
        command = parse_chat_command("$chat --mode roleplay")

        self.assertEqual(command.mode, "roleplay")
        self.assertEqual(command.errors, [])

    def test_rejects_unknown_chat_mode(self):
        command = parse_chat_command("$chat --mode automatic")

        self.assertIn("--mode requires 'agent' or 'roleplay'", command.errors)

    def test_parses_max_effort(self):
        command = parse_chat_command("$chat --effort max")

        self.assertEqual(command.effort, "max")
        self.assertEqual(command.errors, [])


if __name__ == "__main__":
    unittest.main()