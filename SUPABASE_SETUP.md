## Mama's Kitchen Supabase Setup

This project is now set up to use Supabase Postgres.

### What changed

- `app.py` now connects to Supabase through `SUPABASE_DB_URL`
- the MySQL connector fallback was removed
- the app keeps a SQL compatibility layer so existing query strings can still run on Postgres
- the starter schema is in `supabase_schema.sql`

### 1. Create a Supabase project

1. Go to https://supabase.com
2. Create a new project
3. Wait until the database is ready

### 2. Get the Postgres connection string

1. Open your Supabase project
2. Go to `Project Settings`
3. Open `Database`
4. Find the `Connection string`
5. Copy the `URI` or `psql` style Postgres URL

It should look similar to:

```text
postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT-REF].supabase.co:5432/postgres
```

### 3. Create the tables

1. In Supabase, open `SQL Editor`
2. Create a new query
3. Paste the full contents of `supabase_schema.sql`
4. Run it

This creates the tables the current app code expects.

### 4. Install the Python Postgres driver

Run this in your project environment:

```powershell
pip install psycopg2-binary
```

### 5. Set environment variables

The app now auto-loads a local `.env` file on startup.

This project already has a `.env` file with your Supabase URL. If you want to set it manually in PowerShell instead, use:

```powershell
$env:DB_BACKEND="postgres"
$env:SUPABASE_DB_URL="postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT-REF].supabase.co:5432/postgres"
```

If you want to edit the `.env` file directly, set:

```env
DB_BACKEND=postgres
SUPABASE_DB_URL=postgresql://postgres:[YOUR-PASSWORD]@db.[YOUR-PROJECT-REF].supabase.co:5432/postgres
```

### 5b. Create the Storage buckets

This project uploads files to Supabase Storage, not to the database itself.

Create these buckets in `Storage` inside your Supabase project:

- `product-images`
- `mamas_kitchen-documents`

If you want different bucket names, set them in `.env`:

```env
SUPABASE_PRODUCT_IMAGE_BUCKET=your-product-bucket
SUPABASE_DOCUMENT_BUCKET=your-document-bucket
```

The website uses them like this:

- `product-images`: product and variant images
- `mamas_kitchen-documents`: profile pictures, valid IDs, business permits, driver's licenses, delivery proofs

### 6. Start the app

```powershell
python app.py
```

### 7. Test the important flows

After startup, test these first:

1. Registration
2. Login
3. Product creation
4. Cart and checkout
5. Order status updates
6. Messaging
7. Ratings

### Important notes

- The old MySQL auto-schema bootstrap is skipped in Supabase mode.
- `supabase_schema.sql` is a code-based schema built from the tables and columns referenced in the app.
- If your old MySQL database already has live data, that data still needs to be exported and imported into Supabase separately.
- Some older queries in the app were written in MySQL style; `app.py` now translates the common ones for Postgres.

### Optional next step

If you want, I can do the next part too:

1. create a `.env` file for you
2. add `requirements.txt`
3. generate a MySQL-to-Supabase data migration script
