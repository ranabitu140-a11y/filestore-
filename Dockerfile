# Use an official, lightweight Python image
FROM python:3.11-slim

# Stop Python from generating .pyc files and enable real-time terminal output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory inside the container
WORKDIR /app

# Copy your requirements file first to leverage Docker caching
COPY requirements.txt .

# Install your dependencies (like aiogram, pyrogram, motor, etc.)
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your bot's source code into the container
COPY . .

# Command to execute your bot
CMD ["python", "main.py"] 
