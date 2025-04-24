# Stage 1: Build the PyInstaller executable
FROM python:alpine AS builder

# Set working directory
WORKDIR /app

# Install build dependencies
RUN apk add --no-cache \
    binutils

# Copy the script
COPY telegram_cleaner.py .

# Install PyInstaller and telethon
RUN pip install --no-cache-dir pyinstaller telethon

# Build the standalone executable
RUN pyinstaller --onefile --name TelegramCleaner telegram_cleaner.py


# Stage 2: Create the runtime image
FROM alpine:3.20

# Set working directory
WORKDIR /app

# Copy the compressed executable from the builder stage
COPY --from=builder /app/dist/TelegramCleaner .

# Ensure the executable is runnable
RUN chmod +x TelegramCleaner

# Create a volume for persistent files
VOLUME /app/data

# Set UTF-8 for emoji support
ENV LANG=C.UTF-8

# Set the entrypoint to run the executable interactively
ENTRYPOINT ["./TelegramCleaner"]