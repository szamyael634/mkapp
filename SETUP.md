# Petopia Setup Guide

Follow these steps to set up Petopia on a new machine.

## Prerequisites

- Python 3.8 or higher
- Git (optional, for cloning the repo)

## Installation Steps

### 1. Install Python Dependencies

```bash
pip install -r requirements.txt
```

### 2. Set Up Environment Variables

Create a `.env` file in the root folder with the following:

```
DB_BACKEND=postgres
SUPABASE_DB_URL=postgresql://postgres:[PASSWORD]@[HOST]:5432/postgres
GMAIL_USER=your-email@gmail.com
GMAIL_PASSWORD=your-app-specific-password
```

#### Getting Supabase Credentials:
1. Create a Supabase account at https://supabase.com
2. Create a new project
3. Copy the PostgreSQL connection string from "Database" → "Connection string"
4. Paste it in `.env` as `SUPABASE_DB_URL`

#### Getting Gmail App Password:
1. Enable 2-Factor Authentication on your Google account
2. Go to https://myaccount.google.com/apppasswords
3. Generate an app-specific password
4. Use this in `GMAIL_PASSWORD`

### 3. Initialize Database Schema

1. Open your Supabase project
2. Go to **SQL Editor** → **New query**
3. Copy the entire contents of `supabase_schema.sql`
4. Paste and run the query
5. Wait for the schema to be created

### 4. Run the Application

```bash
python app.py
```

The app will be available at: **http://localhost:5000**

## Troubleshooting

**Database connection error?**
- Verify `SUPABASE_DB_URL` is correct in `.env`
- Check that your Supabase project is active

**Email not sending?**
- Verify Gmail credentials in `.env`
- Make sure you used an app-specific password (not your main Google password)

**Missing templates or static files?**
- Ensure you have the `templates/` and `static/` folders in the root directory

## Project Structure

```
Petopia/
├── app.py                 # Main Flask application
├── requirements.txt       # Python dependencies
├── .env                   # Environment variables (don't share!)
├── supabase_schema.sql   # Database schema
├── templates/            # HTML templates
├── static/               # CSS, JavaScript, images
└── migrations/           # Database migrations
```

## Notes

- Never commit `.env` to version control
- Keep your database credentials secure
- Use `requirements.txt` to manage dependencies
