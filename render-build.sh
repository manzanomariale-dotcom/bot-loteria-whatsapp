#!/usr/bin/env bash
# exit on error
set -o errexit

apt-get update && apt-get install -y tesseract-ocr tesseract-ocr-spa libgl1-mesa-glx
pip install -r requirements.txt