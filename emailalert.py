from email.message import EmailMessage
import smtplib
from dotenv import load_dotenv
import os

def sendalert(receiver_email=None,
              message = "Hello! This email was sent using Python and Gmail SMTP.",
              content = None ,
              subject = None,
              ):

    # 1. Configure account and message details
    load_dotenv()
    receiver_email = receiver_email or os.getenv("ALERT_RECIPIENT")
    if not receiver_email:
        raise ValueError("Set ALERT_RECIPIENT before sending email")
    sender_email = os.getenv("SENDER_EMAIL")
    app_password = os.getenv("EMAIL_APP_PASSWORD")  # Your 16-character App Password

    msg = EmailMessage()
    msg["Subject"] = subject or "Automated Email from Python"
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg.set_content(message + "\n" + (content or "This is a test."))

    # 2. Connect to Gmail's SMTP server and send
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender_email, app_password)
        server.send_message(msg)

    print("Email sent successfully!")

if __name__ == "__main__":
    sendalert()