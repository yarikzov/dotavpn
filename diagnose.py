#!/usr/bin/env python
"""Diagnose startup issues"""

import os
import sys

print("=" * 50)
print("DIAGNOSTIC - VpnDota")
print("=" * 50)

# Check Python version
print(f"Python version: {sys.version}")

# Check current directory
print(f"Current directory: {os.getcwd()}")

# Check required files
required_files = ['bot.py', 'db.py', 'server.py', 'crypto_api.py', 'xui_api.py']
for f in required_files:
    exists = os.path.exists(f)
    print(f"  {f}: {'✓' if exists else '✗'}")

# Check webapp folder
if os.path.exists('webapp'):
    print("webapp folder: ✓")
    files = os.listdir('webapp')
    print(f"  Files: {files}")
    if 'index.html' in files:
        print("  index.html: ✓")
    else:
        print("  index.html: ✗ MISSING!")
else:
    print("webapp folder: ✗ MISSING!")

# Check .env file
if os.path.exists('.env'):
    print(".env file: ✓")
    with open('.env', 'r') as f:
        env_content = f.read()
        if 'BOT_TOKEN' in env_content and 'YOUR_BOT_TOKEN' not in env_content:
            print("  BOT_TOKEN: configured")
        else:
            print("  BOT_TOKEN: MISSING or not configured!")
else:
    print(".env file: ✗ MISSING!")

# Try to import modules
try:
    from flask import Flask
    print("Flask: ✓")
except ImportError as e:
    print(f"Flask: ✗ - {e}")

try:
    from db import Database
    print("db module: ✓")
except ImportError as e:
    print(f"db module: ✗ - {e}")

print("=" * 50)
print("Try running: python server.py")