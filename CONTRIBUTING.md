# Contributing to tvscrn

Thanks for your interest in contributing! This guide covers development setup, code style, and how to customize tvscrn for your needs.

## Development Setup

```bash
# Clone the repository
git clone https://github.com/tvscrn/tvscrn.git
cd tvscrn

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run in development mode
FLASK_ENV=development python3 app.py
```

## Project Structure

```
tvscrn/
├── app.py                  # Main application (Flask + SocketIO)
├── requirements.txt        # Python dependencies
├── docker-compose.yml      # Docker deployment
├── Dockerfile              # Container build
├── entrypoint.sh           # Docker entry point
├── mediamtx.yml            # WebRTC server config
├── templates/              # Jinja2 HTML templates
│   ├── dashboard.html      # Main device list
│   ├── login.html          # Login page
│   ├── device_control.html # VNC remote control
│   ├── settings.html       # Application settings
│   ├── users.html          # User management (admin)
│   └── audit_log.html      # Audit trail (admin)
├── data/                   # Runtime data (SQLite DB, config)
└── docs/                   # Documentation
```

## Code Style

- **Python:** Follow PEP 8. Use descriptive variable names.
- **HTML:** Use semantic HTML5. Keep templates clean and readable.
- **CSS:** Use CSS custom properties for theming. See `templates/dashboard.html` for the design system.
- **JavaScript:** Vanilla JS only — no frameworks. Keep it simple and compatible.

## Rebranding Guide

tvscrn is designed to be easily rebranded. Here's how to customize it for your organization.

### Step 1: Find All Branding Points

Search for `tvscrn` in all files:

```bash
grep -rn 'tvscrn' --include='*.py' --include='*.html' --include='*.yml' --include='*.md' .
```

### Step 2: Update Application Name

| File | What to Change |
|------|---------------|
| `app.py` | Logger name, email templates, startup banner |
| `templates/login.html` | Page title, heading |
| `templates/dashboard.html` | Navbar brand, footer text |
| `templates/settings.html` | Settings page title |
| `templates/users.html` | Page title, navbar |
| `templates/audit_log.html` | Page title, navbar |

### Step 3: Update Footer Attribution

In `templates/dashboard.html`, find the footer:

```html
<div class="footer">
    Powered by <a href="https://github.com/tvscrn/tvscrn">tvscrn</a>
</div>
```

Replace with your branding while keeping the attribution:

```html
<div class="footer">
    YourBrand — Powered by <a href="https://github.com/tvscrn/tvscrn">tvscrn</a>
</div>
```

### Step 4: Update Email Templates

In `app.py`, search for email HTML templates. Update the branding while keeping the functionality.

### Step 5: Update Docker

In `docker-compose.yml`:
- Change container name: `container_name: your-app-name`
- Change service name: `services: your-service:`

In `Dockerfile`:
- Update any branding in comments or labels

### Step 6: Update Documentation

- Update `README.md` with your organization's name
- Update any setup guides
- Keep the tvscrn attribution in the license section

### Branding Checklist

- [ ] Application name updated in all templates
- [ ] Footer includes "Powered by tvscrn" with GitHub link
- [ ] Email templates updated
- [ ] Docker configuration updated
- [ ] Documentation updated
- [ ] License file preserved (MIT + attribution requirement)

## Reporting Issues

When reporting bugs, please include:
1. tvscrn version (check the footer)
2. Python version (`python3 --version`)
3. Operating system
4. Steps to reproduce
5. Expected vs actual behavior
6. Relevant logs (`journalctl -u tvscrn` or `docker compose logs`)

## Pull Requests

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Test thoroughly
5. Update documentation if needed
6. Submit a pull request

### PR Guidelines

- Keep changes focused — one feature/fix per PR
- Include tests if applicable
- Update README.md for new features
- Follow existing code style
- Add yourself to CONTRIBUTORS.md (optional)

## License

By contributing, you agree that your contributions will be licensed under the MIT License with the attribution requirement.
