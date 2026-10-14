import unittest
from unittest.mock import Mock, patch

from providers import web_page_query


class WebPageQueryTests(unittest.TestCase):
    @staticmethod
    def _response(body: bytes, content_type="text/html", status=200):
        response = Mock()
        response.is_redirect = False
        response.is_permanent_redirect = False
        response.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        response.encoding = "utf-8"
        response.iter_content.return_value = [body]
        response.raise_for_status.side_effect = None if status == 200 else RuntimeError("HTTP error")
        return response

    def test_fetches_public_html_and_removes_script(self):
        response = self._response(
            b'<html><title>Example</title><body><main>Hello world</main>'
            b'<a href="/section/4">Section 4</a><script>ignore me</script></body></html>'
        )
        public_address = [(None, None, None, None, ("93.184.216.34", 443))]
        with patch("providers.web_page_query.socket.getaddrinfo", return_value=public_address), \
                patch("providers.web_page_query.requests.get", return_value=response):
            page = web_page_query.fetch_web_page("https://example.com/article")

        self.assertEqual(page["title"], "Example")
        self.assertIn("Hello world", page["text"])
        self.assertNotIn("ignore me", page["text"])
        self.assertEqual(page["links"], [{
            "title": "Section 4",
            "url": "https://example.com/section/4",
        }])
        response.close.assert_called_once()

    def test_rejects_private_destination_without_request(self):
        private_address = [(None, None, None, None, ("127.0.0.1", 80))]
        with patch("providers.web_page_query.socket.getaddrinfo", return_value=private_address), \
                patch("providers.web_page_query.requests.get") as get:
            with self.assertRaisesRegex(web_page_query.WebPageUnavailable, "non-public"):
                web_page_query.fetch_web_page("http://example.test/private")

        get.assert_not_called()

    def test_revalidates_redirect_destination(self):
        redirect = self._response(b"")
        redirect.is_redirect = True
        redirect.headers = {"Location": "http://127.0.0.1/admin"}
        public_address = [(None, None, None, None, ("93.184.216.34", 443))]
        with patch("providers.web_page_query.socket.getaddrinfo", return_value=public_address), \
                patch("providers.web_page_query.requests.get", return_value=redirect):
            with self.assertRaisesRegex(web_page_query.WebPageUnavailable, "non-public"):
                web_page_query.fetch_web_page("https://example.com/redirect")

        redirect.close.assert_called_once()

    def test_extracts_pdf_text(self):
        response = self._response(b"%PDF example", "application/pdf")
        page = Mock()
        page.extract_text.return_value = "Section 4 text\nSection 5 text"
        reader = Mock(pages=[page], metadata={"/Title": "Official ordinance"})
        public_address = [(None, None, None, None, ("93.184.216.34", 443))]
        with patch("providers.web_page_query.socket.getaddrinfo", return_value=public_address), \
                patch("providers.web_page_query.requests.get", return_value=response), \
                patch("providers.web_page_query.PdfReader", return_value=reader):
            result = web_page_query.fetch_web_page("https://example.com/file.pdf")

        self.assertEqual(result["title"], "Official ordinance")
        self.assertIn("Section 4 text", result["text"])
        self.assertEqual(result["content_type"], "application/pdf")
        response.close.assert_called_once()

    def test_rejects_unsupported_binary_content(self):
        response = self._response(b"binary", "application/octet-stream")
        public_address = [(None, None, None, None, ("93.184.216.34", 443))]
        with patch("providers.web_page_query.socket.getaddrinfo", return_value=public_address), \
                patch("providers.web_page_query.requests.get", return_value=response):
            with self.assertRaisesRegex(web_page_query.WebPageUnavailable, "Unsupported"):
                web_page_query.fetch_web_page("https://example.com/file.bin")


if __name__ == "__main__":
    unittest.main()