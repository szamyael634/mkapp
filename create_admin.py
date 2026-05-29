#!/usr/bin/env python3
"""
Script to create admin account in Mama's Kitchen Supabase database
"""

import requests
import json

# Supabase project details
PROJECT_REF = "ejujujiamdrftqkdqaqg"
SUPABASE_URL = f"https://{PROJECT_REF}.supabase.co"
ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImVqdWp1amlhbWRyZnRxa2RxYXFnIiwicm9sZSI6ImFub24iLCJpYXQiOjE2OTI5MTc3NjgsImV4cCI6MjAwODQ5Nzc2OH0.K5-_Cxpn9H0vPp5Y8Z9W9X9Y9Z9X9Y9Z9X9Y9Z9X9Y8"

# Admin user data
admin_data = {
    "fullname": "Admin User",
    "email": "admin@mamaskitchen.com",
    "password": "pbkdf2:sha256:600000$KXBHzZXqLJv1QmH9$3b8a6f8d1c2a5e9f7b4d1c8a3e5f2b9d1a6c4e7f8b2d5a9c1e3f6b8d1a4c7e",
    "role": "admin",
    "status": "active",
    "phone": "+1-800-MAMASKITCHEN",
    "address": "Mama's Kitchen Headquarters"
}

headers = {
    "apikey": ANON_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

try:
    # Try to insert the admin user
    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/users",
        headers=headers,
        json=admin_data,
        timeout=10
    )
    
    if response.status_code in [200, 201]:
        print("✓ Admin account created successfully!")
        print(f"✓ Email: {admin_data['email']}")
        print(f"✓ Role: {admin_data['role']}")
        print(f"✓ Password Hash: {admin_data['password'][:30]}...")
        result = response.json()
        print(f"✓ User ID: {result[0].get('id', 'N/A')}")
    elif response.status_code == 409:
        print("✓ Admin account already exists")
        print(f"✓ Email: {admin_data['email']}")
    else:
        print(f"✗ Error: {response.status_code}")
        print(f"✗ Response: {response.text}")
        
except requests.exceptions.RequestException as e:
    print(f"✗ Request failed: {e}")
except Exception as e:
    print(f"✗ Unexpected error: {e}")
