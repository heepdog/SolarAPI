from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import emailalert


class EmailAlertConfigurationTests(unittest.TestCase):
    def test_missing_recipient_fails_before_connecting_to_smtp(self) -> None:
        with (
            patch.object(emailalert, "load_dotenv"),
            patch.dict(os.environ, {"ALERT_RECIPIENT": ""}),
        ):
            with self.assertRaisesRegex(ValueError, "ALERT_RECIPIENT"):
                emailalert.sendalert()

    def test_sends_plain_text_and_html_alternatives(self) -> None:
        with (
            patch.object(emailalert, "load_dotenv"),
            patch.object(emailalert.smtplib, "SMTP_SSL") as smtp_ssl,
            patch.dict(
                os.environ,
                {
                    "SENDER_EMAIL": "sender@example.com",
                    "EMAIL_APP_PASSWORD": "test-password",
                },
            ),
        ):
            emailalert.sendalert(
                receiver_email="recipient@example.com",
                message="",
                content="Plain text alert",
                html_content="<p>HTML alert</p>",
            )

        message = smtp_ssl.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        self.assertEqual(
            [part.get_content_type() for part in message.iter_parts()],
            ["text/plain", "text/html"],
        )


if __name__ == "__main__":
    unittest.main()
