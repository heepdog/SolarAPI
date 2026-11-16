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


if __name__ == "__main__":
    unittest.main()
