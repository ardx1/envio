import smtplib
import os
import time
import argparse
import random
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from gevent.pool import Pool
from gevent import monkey
from gevent.lock import RLock
from collections import defaultdict
from itertools import cycle

# Patch standard library for gevent compatibility
monkey.patch_all()

# Set up logging to show only essential messages
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Function to validate an email address
def is_valid_email(email):
    return '@' in email and '.' in email

# Function to load SMTP details from a file
def load_smtp_details(filename):
    try:
        with open(filename, 'r') as file:
            lines = file.readlines()
        smtp_details = [tuple(line.strip().split('|')) for line in lines if line.strip() and not line.strip().startswith('#')]
        return smtp_details
    except Exception as e:
        logging.error(f"Error loading SMTP details from {filename}: {e}")
        return []

# Function to save SMTP details to a file
def save_smtp_details(filename, smtp_details_list):
    try:
        with open(filename, 'w') as file:
            for smtp in smtp_details_list:
                file.write('|'.join(smtp) + '\n')
    except Exception as e:
        logging.error(f"Error saving SMTP details to {filename}: {e}")

# Function to load email addresses from a file
def load_email_addresses(filename):
    try:
        with open(filename, 'r') as file:
            email_addresses = {line.strip() for line in file if is_valid_email(line.strip())}
        return email_addresses
    except Exception as e:
        logging.error(f"Error loading email addresses from {filename}: {e}")
        return set()

# Function to load subjects from a file
def load_subjects(filename):
    try:
        with open(filename, 'r') as file:
            subjects = [line.strip() for line in file if line.strip()]
        return subjects
    except Exception as e:
        logging.error(f"Error loading subjects from {filename}: {e}")
        return []

# Function to load HTML body from a file
def load_html_body(filename):
    try:
        with open(filename, 'r') as file:
            html_body = file.read()
        return html_body
    except Exception as e:
        logging.error(f"Error loading HTML body from {filename}: {e}")
        return ""

# Function to create the email message
def create_message(subject, body, from_email, to_email, attachment_path=None):
    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = subject

    # Attach the body in HTML format
    msg.attach(MIMEText(body, 'html'))

    # Attach the file if provided
    if attachment_path:
        try:
            with open(attachment_path, 'rb') as attachment:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(attachment.read())
                encoders.encode_base64(part)
                part.add_header(
                    'Content-Disposition',
                    f'attachment; filename={os.path.basename(attachment_path)}',
                )
                msg.attach(part)
        except Exception as e:
            logging.error(f"Error attaching file {attachment_path}: {e}")

    return msg

# Function to send an email
def send_email(smtp_detail, subject, body, from_email, to_email, attachment_path=None):
    smtp_server, smtp_port, email_user, email_password = smtp_detail
    smtp_port = int(smtp_port)

    logging.info(f"Sending email from {from_email} to {to_email} using SMTP server {smtp_server}:{smtp_port}")

    for attempt in range(10):
        try:
            msg = create_message(subject, body, from_email, to_email, attachment_path)
            logging.info(f"Attempt {attempt + 1}: Trying to send email")

            # Attempt STARTTLS
            logging.info(f"Attempting STARTTLS connection to {smtp_server}:{smtp_port}")
            try:
                with smtplib.SMTP(smtp_server, smtp_port, timeout=20) as server:
                    server.set_debuglevel(0)  # Disable debug output
                    server.starttls()
                    server.login(email_user, email_password)
                    server.sendmail(from_email, to_email, msg.as_string())
                    logging.info(f"Email sent successfully from {from_email} to {to_email} (STARTTLS)!")
                    return True
            except (smtplib.SMTPAuthenticationError, smtplib.SMTPException) as e:
                logging.error(f"STARTTLS failed: {e}")

            # Attempt TLS
            logging.info(f"STARTTLS failed, trying TLS connection to {smtp_server}:{smtp_port}")
            try:
                with smtplib.SMTP(smtp_server, smtp_port, timeout=20) as server:
                    server.set_debuglevel(0)  # Disable debug output
                    server.login(email_user, email_password)
                    server.sendmail(from_email, to_email, msg.as_string())
                    logging.info(f"Email sent successfully from {from_email} to {to_email} (TLS)!")
                    return True
            except (smtplib.SMTPAuthenticationError, smtplib.SMTPException) as e:
                logging.error(f"TLS attempt failed: {e}")

            # Attempt SSL/TLS
            logging.info(f"TLS failed, trying SSL/TLS connection to {smtp_server}:{smtp_port}")
            try:
                with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30) as server:
                    server.set_debuglevel(0)  # Disable debug output
                    server.login(email_user, email_password)
                    server.sendmail(from_email, to_email, msg.as_string())
                    logging.info(f"Email sent successfully from {from_email} to {to_email} (SSL/TLS)!")
                    return True
            except (smtplib.SMTPAuthenticationError, smtplib.SMTPException) as e:
                logging.error(f"SSL/TLS attempt failed: {e}")

        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {e}")

        # Wait between attempts
        time.sleep(10)

    logging.error(f"Failed to send email after {attempt + 1} attempts.")
    return False


# Task to handle the email sending process
def email_task(to_email, smtp_cycle, subject, body, attachment_path=None):
    global failed_smtp, failed_smtp_lock, smtp_failures_to_remove
    smtp_detail = next(smtp_cycle)
    from_email = smtp_detail[2]

    if send_email(smtp_detail, subject, body, from_email, to_email, attachment_path):
        remove_email_from_list('senders.txt', to_email)
        logging.info(f"Email successfully sent to {to_email}")
    else:
        with failed_smtp_lock:
            failed_smtp[smtp_detail] += 1
            if failed_smtp[smtp_detail] >= 10:
                # Mark SMTP for removal
                smtp_failures_to_remove.add(smtp_detail)
                logging.warning(f"SMTP {smtp_detail} will be removed after 10 failures.")
        # Only this thread sleeps for 3 seconds before retrying
        time.sleep(3)

# Function to remove an email from the senders list
def remove_email_from_list(filename, email):
    try:
        with open(filename, 'r') as file:
            emails = {line.strip() for line in file if line.strip() and is_valid_email(line.strip())}
        if email in emails:
            emails.remove(email)
            with open(filename, 'w') as file:
                for e in emails:
                    file.write(f"{e}\n")
            logging.info(f"Removed {email} from list.")
    except Exception as e:
        logging.error(f"Error removing {email} from {filename}: {e}")

# Function to remove an SMTP from the list after it fails 10 times
def remove_smtp_from_list(smtp_detail):
    smtp_to_remove = f"{smtp_detail[0]}|{smtp_detail[1]}|{smtp_detail[2]}|{smtp_detail[3]}"
    try:
        # Read the existing SMTP details
        with open('smtps.txt', 'r') as file:
            smtp_list = file.readlines()
        
        # Write all except the one to be removed
        with open('smtps.txt', 'w') as file:
            for line in smtp_list:
                if line.strip() != smtp_to_remove:
                    file.write(line)
        logging.info(f"Removed SMTP {smtp_detail} from list.")
    except Exception as e:
        logging.error(f"Error removing SMTP {smtp_detail} from list: {e}")

# Function to start the email sending process with concurrency
def send_emails(to_emails, body, smtp_details_list, subject, attachment_path=None, threads=10):
    global failed_smtp, failed_smtp_lock, smtp_failures_to_remove

    if not to_emails:
        logging.warning("No emails to send.")
        return

    if not smtp_details_list:
        logging.warning("No SMTP details available.")
        return

    failed_smtp = defaultdict(int)
    failed_smtp_lock = RLock()
    smtp_failures_to_remove = set()

    smtp_cycle = cycle(smtp_details_list)

    pool = Pool(threads)
    pool.map(lambda email: email_task(email, smtp_cycle, subject, body, attachment_path), to_emails)

    pool.join()

    with failed_smtp_lock:
        for smtp_detail in smtp_failures_to_remove:
            remove_smtp_from_list(smtp_detail)

    if smtp_failures_to_remove:
        logging.info("Some SMTP servers have been removed due to repeated failures.")

    # Ensure all SMTP servers are still valid
    check_remaining_smtp()

# Function to check remaining SMTP servers and log warnings if none are available
def check_remaining_smtp():
    try:
        with open('smtps.txt', 'r') as file:
            remaining_smtp_details = [line.strip() for line in file if line.strip()]
        if not remaining_smtp_details:
            logging.warning("No remaining SMTP servers.")
    except Exception as e:
        logging.error(f"Error checking remaining SMTP servers: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send bulk emails with concurrent threads and SMTP handling.")
    parser.add_argument('-t', '--threads', type=int, default=10, help='Number of concurrent threads (default: 10)')
    parser.add_argument('-a', '--attachment', type=str, default=None, help='Path to the attachment file (optional)')
    parser.add_argument('email_file', type=str, nargs='?', default='senders.txt', help='Path to the email list file (default: senders.txt)')
    parser.add_argument('html_file', type=str, nargs='?', default='1244.html', help='Path to the HTML letter file (optional)')

    args = parser.parse_args()
    
    to_emails = load_email_addresses(args.email_file)
    body = load_html_body(args.html_file) if args.html_file else ""
    attachment_path = args.attachment

    smtp_details_list = load_smtp_details('smtps.txt')
    subjects_list = load_subjects('t.txt')

    if subjects_list:
        subject = random.choice(subjects_list)
    else:
        logging.warning("No subjects available.")
        subject = "No Subject"

    send_emails(to_emails, body, smtp_details_list, subject, attachment_path, args.threads)
