import unittest

from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from app.main import add_security_headers, app


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

    def test_cross_site_mutations_are_rejected_before_input_processing(self) -> None:
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        for method, path in (("POST", "/api/jobs"), ("PATCH", "/api/settings"), ("DELETE", "/api/jobs/absent")):
            response = client.request(method, path, headers={"origin": "https://untrusted.example"}, content=b"invalid body")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_local_validation_errors_do_not_echo_input_or_fail_to_encode_nonfinite_numbers(self) -> None:
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        response = client.post(
            "/api/templates",
            headers={"origin": "http://127.0.0.1:8000", "content-type": "application/json"},
            content='{"name":"private-template-name", "params":{"box_distance":1e999}}',
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["problem"]["code"], "invalid_request")
        self.assertNotIn("private-template-name", response.text)
        for error in response.json()["detail"]:
            self.assertNotIn("input", error)
            self.assertNotIn("ctx", error)


if __name__ == "__main__":
    unittest.main()
