# myPhotoHub

A lightweight, self-hosted photo library for Windows. No Docker, no Node.js, no frontend build step.

## Requirements

- Windows 10/11
- Python 3.11 or newer from https://www.python.org/downloads/windows/
- Internet access the first time you launch it, so pip can install Flask and Pillow

During Python setup, enable **Add Python to PATH**. If the `py` launcher is available, the included launcher will use it.

## Run it

1. Extract this folder somewhere persistent, e.g. `C:\myPhotoHub`.
2. Double-click `run-myPhotoHub.bat`.
3. Wait for dependencies to install the first time.
4. Open `http://127.0.0.1:5055`.
5. Create your admin username and a password of at least 12 characters.
6. Open **Admin dashboard** to manage separate accounts for family or friends, profiles, storage, backups, and server settings.

Keep the console window open while using myPhotoHub. Closing it stops the web app; your photos and database remain on disk.

## Shared storage across versions

Every version stores its library in the same Windows user data folder by default:

- `%LOCALAPPDATA%\myPhotoHub\photos\` — original uploaded images
- `%LOCALAPPDATA%\myPhotoHub\thumbnails\` — generated thumbnails
- `%LOCALAPPDATA%\myPhotoHub\instance\myPhotoHub.db` — users, photo ownership, albums, favorites and metadata
- `%LOCALAPPDATA%\myPhotoHub\instance\config.json` — session secret and setup configuration

This means you can extract a newer myPhotoHub ZIP into a different application folder and it will still use the same accounts and library. On first launch, it also copies legacy `instance`, `photos`, and `thumbnails` folders from beside `app.py` into the shared location when the shared location does not already exist. The old files are left in place as a safety copy. Do not run two myPhotoHub versions at the same time; they share one database and use the same port.

Back up the whole `%LOCALAPPDATA%\myPhotoHub\` folder together. To choose another shared location, set `myPhotoHub_DATA_DIR` before launching myPhotoHub.

## Access from other devices

- **Home LAN:** find the Windows server's LAN IPv4 address with `ipconfig`, then open `http://SERVER-IP:5055` on a device on the same network.
- **Tailscale:** with Tailscale already installed and connected on both the server and your phone, open `http://SERVER-TAILSCALE-IP:5055` or the server's Tailscale MagicDNS name with `:5055`.

Do not port-forward 5055 to the public internet. The app uses password login, but its HTTP connection is not encrypted by itself. Use it on your trusted LAN or through Tailscale. If you need access from outside your tailnet, put it behind a properly configured HTTPS reverse proxy rather than exposing this app directly.

## Windows Firewall

If other devices cannot connect, Windows Firewall may be blocking Python. Create an inbound TCP rule for port 5055 limited to **Private** networks, or allow Python on your trusted private network only. Do not create a public-network rule.

## Uploads and formats

Supports JPG/JPEG, PNG, WebP, GIF, BMP, TIF and TIFF. HEIC/HEIF and RAW formats are not currently supported. The server retains a 512 MB hard limit per request, while the browser automatically divides a large selection into sequential batches of up to 40 files and roughly 96 MB each. The upload window shows progress. Keep the browser tab open until it completes; if the connection fails, already-saved batches remain in the library and can be skipped manually when retrying. You can change the server request cap with `myPhotoHub_MAX_UPLOAD_MB` before launch.

## Current features

- Separate username/password accounts with private per-user libraries
- Admin dashboard with account creation, enable/disable, admin roles, password resets, account deletion, and library deletion
- User profile pictures and self-service password changes
- Storage and disk statistics, configurable per-request upload limits, recent activity log, and captured server errors
- Downloadable ZIP backups of the database, photos, thumbnails, avatars, and configuration
- Password setup and login
- Multi-file uploads with automatic batching and progress tracking
- Gallery with search
- Favorites
- Editable display titles
- Albums: create, add photos, remove photos, delete albums
- Image preview, metadata dimensions, original filename
- Responsive desktop/mobile layout

## Notes

- Administrators can manage accounts and delete other users’ libraries. Standard users cannot view or modify another account’s photos or albums. Admin actions that delete data require a browser confirmation.
- Backups contain the SQLite database and all myPhotoHub media/configuration. Keep an extra copy off the server. The dashboard creates and downloads backups; restoring one currently requires stopping myPhotoHub and restoring the files manually.
- Admin controls apply to the myPhotoHub application and its data, not Windows itself. Stop/restart the server using the myPhotoHub console or Windows process controls.
- The app stores photos as ordinary files and metadata in SQLite.
- Uploads are copied into myPhotoHub; they do not delete or alter the source copies.
- For safety, this version does not automatically shut down Windows or delete original photos without a confirmation step.
