#!/usr/bin/env python3
"""List available Gemini models and their supported methods."""

import os
import sys
from google import genai

def main():
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("Error: GOOGLE_API_KEY environment variable not set")
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    print("Available Gemini models:\n")

    for model in client.models.list():
        # Check if it supports generateContent (needed for our use case)
        if 'generateContent' in model.supported_generation_methods:
            print(f"✓ {model.name}")
            print(f"  Display name: {model.display_name}")
            print(f"  Methods: {', '.join(model.supported_generation_methods)}")
            print()

if __name__ == "__main__":
    main()
