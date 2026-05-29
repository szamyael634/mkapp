#!/usr/bin/env python3
"""
Mama's Kitchen - Automated Deployment Script
Handles deployment to Vercel and setup of environment variables
"""

import subprocess
import sys
import json
import os
from pathlib import Path

def run_command(cmd, description):
    """Run a shell command and handle errors"""
    print(f"\n{'='*60}")
    print(f"📦 {description}")
    print(f"{'='*60}")
    print(f"Running: {cmd}")
    
    try:
        result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
        print(result.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Error: {e.stderr}")
        return False

def check_prerequisites():
    """Check if required tools are installed"""
    print("\n📋 Checking prerequisites...\n")
    
    tools = {
        "vercel": "vercel --version",
        "npm": "npm --version",
        "git": "git --version",
        "python": "python --version"
    }
    
    missing = []
    for tool, cmd in tools.items():
        result = subprocess.run(cmd, shell=True, capture_output=True)
        if result.returncode == 0:
            print(f"✅ {tool}: {result.stdout.decode().strip()}")
        else:
            print(f"❌ {tool}: NOT FOUND")
            missing.append(tool)
    
    if missing:
        print(f"\n⚠️  Missing tools: {', '.join(missing)}")
        print("Please install these tools before continuing.")
        return False
    
    return True

def get_environment_variables():
    """Prompt user for environment variables"""
    print("\n📝 Environment Variables Configuration")
    print("="*60)
    
    env_vars = {}
    
    # Read from .env file if exists
    env_file = Path(".env")
    if env_file.exists():
        print("📂 Found .env file, reading values...")
        with open(env_file) as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    key, value = line.strip().split("=", 1)
                    env_vars[key] = value
    
    required_vars = [
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_DB_URL"
    ]
    
    for var in required_vars:
        current = env_vars.get(var, "")
        if current:
            print(f"✅ {var}: {current[:30]}...")
        else:
            print(f"⚠️  {var}: Not configured")
    
    return env_vars

def setup_vercel():
    """Setup Vercel CLI"""
    print("\n🔐 Vercel Setup")
    print("="*60)
    
    # Check if already logged in
    result = subprocess.run("vercel whoami", shell=True, capture_output=True)
    if result.returncode == 0:
        print(f"✅ Already logged in as: {result.stdout.decode().strip()}")
        return True
    else:
        print("❌ Not logged in to Vercel")
        print("Please run: vercel login")
        return False

def create_deployment_checklist():
    """Create pre-deployment checklist"""
    checklist = """
# Pre-Deployment Checklist

## Backend (Flask)
- [ ] All dependencies in requirements.txt
- [ ] Environment variables configured in Vercel
- [ ] Database migrations applied
- [ ] Admin account created
- [ ] API endpoints tested locally
- [ ] CORS configured
- [ ] Error handling implemented

## Mobile App (Flutter)
- [ ] Android build tested
- [ ] iOS build tested (if on macOS)
- [ ] API endpoints updated to production URL
- [ ] App version bumped
- [ ] Signing keys configured

## Supabase
- [ ] All tables created
- [ ] Indexes optimized
- [ ] Row-level security policies set
- [ ] Backups configured

## General
- [ ] Git repository up to date
- [ ] All tests passing
- [ ] Documentation updated
- [ ] Screenshots prepared for app stores
    """
    
    with open("DEPLOYMENT_CHECKLIST.md", "w") as f:
        f.write(checklist)
    print("✅ Created DEPLOYMENT_CHECKLIST.md")

def main():
    """Main deployment flow"""
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║     Mama's Kitchen - Deployment Script                   ║
    ║     Deploying Flask Backend to Vercel                    ║
    ╚═══════════════════════════════════════════════════════════╝
    """)
    
    # Check prerequisites
    if not check_prerequisites():
        sys.exit(1)
    
    # Get environment variables
    env_vars = get_environment_variables()
    
    # Setup Vercel
    if not setup_vercel():
        print("\n⚠️  Please login to Vercel first:")
        print("   vercel login")
        sys.exit(1)
    
    # Confirm deployment
    print("\n" + "="*60)
    print("⚠️  Ready to deploy to Vercel")
    print("="*60)
    response = input("\nDeploy now? (yes/no): ").lower()
    
    if response != "yes":
        print("❌ Deployment cancelled")
        sys.exit(0)
    
    # Deploy
    if run_command("vercel --prod", "Deploying to Vercel"):
        print("\n✅ Deployment successful!")
        print("\nNext steps:")
        print("1. Go to Vercel dashboard to verify deployment")
        print("2. Test API endpoints")
        print("3. Update Flutter app API URL")
        print("4. Deploy Flutter app to app stores")
    else:
        print("\n❌ Deployment failed")
        sys.exit(1)
    
    # Create checklist
    create_deployment_checklist()
    
    print("\n" + "="*60)
    print("✅ Deployment script completed!")
    print("="*60)

if __name__ == "__main__":
    main()
