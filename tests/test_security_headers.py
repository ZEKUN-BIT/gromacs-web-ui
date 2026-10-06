import unittest

from starlette.requests import Request
from starlette.responses import Response

from app.main import add_security_headers


class SecurityHeaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_security_headers_are_added_to_responses(self) -> None:
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

        async def call_next(_request: Request) -> Response:
            return Response("ok")

        response = await add_security_headers(request, call_next)

        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")


if __name__ == "__main__":
    unittest.main()
