# Mama's Kitchen Email Setup Guide

## Overview
The Mama's Kitchen application now sends automatic emails to users when their account is approved or rejected by the admin.

## Email Features
- ✅ **Approval Email**: Sent when admin clicks "Approve" button
- ✅ **Rejection Email**: Sent when admin clicks "Reject" button
- ✅ **HTML & Plain Text**: Both formats included for better email client compatibility
- ✅ **Works for all roles**: Customers, Sellers, and Riders

## Gmail Setup Instructions

### Step 1: Enable 2-Factor Authentication (Required)
1. Go to https://myaccount.google.com/security
2. Scroll to "2-Step Verification" and click on it
3. Follow the steps to enable 2FA with your phone

### Step 2: Create an App Password
1. Go to https://myaccount.google.com/apppasswords
2. Select "Mail" as the app
3. Select "Windows Computer" (or your OS) as the device
4. Google will generate a 16-character password
5. **Copy this password - you'll need it in Step 3**

### Step 3: Set Environment Variables

**On Windows (PowerShell):**
```powershell
$env:GMAIL_USER = "your-email@gmail.com"
$env:GMAIL_PASSWORD = "your-16-char-app-password"
```

**On Windows (Command Prompt):**
```cmd
set GMAIL_USER=your-email@gmail.com
set GMAIL_PASSWORD=your-16-char-app-password
```

**Alternative: Create a `.env` file**
Create a `.env` file in your Mama's Kitchen root directory:
```
GMAIL_USER=your-email@gmail.com
GMAIL_PASSWORD=your-16-char-app-password
```

Then add this to app.py (if using .env):
```python
from dotenv import load_dotenv
load_dotenv()
```

### Step 4: Restart Flask App
```bash
python app.py
```

## Testing the Email System

### Send Test Email (Admin Approval):
1. Go to Admin Dashboard → Users Section
2. Click "Approve" on any pending user
3. Check the user's email inbox
4. Should receive an approval email within 1-2 minutes

### Send Test Email (Rejection):
1. Go to Admin Dashboard → Users Section
2. Click "Reject" on any pending user
3. Check the user's email inbox
4. Should receive a rejection email within 1-2 minutes

## Email Templates

### Approval Email
- **Subject**: "Mama's Kitchen Account Approved - [User Role]"
- **Content**: Welcome message with link to login

### Rejection Email
- **Subject**: "Mama's Kitchen Account Approved - [User Role]"
- **Content**: Explanation of rejection and instructions to reapply

## Troubleshooting

### Email Not Sending?
1. Check Flask console for error messages
2. Verify GMAIL_USER and GMAIL_PASSWORD are correctly set
3. Ensure Gmail credentials are valid
4. Check that 2FA is enabled on Gmail account

### "Connection refused" Error
- Gmail SMTP may be blocked: Check firewall settings
- Try using a different network/VPN

### "Invalid credentials" Error
- Wrong app password: Regenerate from https://myaccount.google.com/apppasswords
- Make sure using Gmail account, not custom domain email

## Code Reference

**Email Function Location**: `app.py` lines ~985-1072
**Email Sending Routes**:
- `/update_user_status` (Approval) - Line ~5220
- `/reject_user` (Rejection) - Line ~5268

## Security Notes
- Never commit `.env` file or passwords to git
- App passwords are safer than actual Gmail passwords
- App passwords can only access Gmail, not other account settings

## Production Considerations
- For production, consider using a professional email service (SendGrid, AWS SES, etc.)
- Current setup is suitable for development/testing only
- May need to adjust rate limits for high-volume emails
