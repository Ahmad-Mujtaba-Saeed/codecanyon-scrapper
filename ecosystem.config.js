// pm2 process definition for the research dashboard.
//
//   pm2 start ecosystem.config.js
//   pm2 logs codecanyon-research
//   pm2 save
//
// Notes that matter for this particular app:
//
//   watch MUST stay false. A crawl runs in a background thread inside this
//   process; a file-watch restart would kill it mid-run. Interrupted runs are
//   resumable (`python run.py scrape --resume <RUN_ID>`), but silently losing
//   a half-finished crawl is worse than a manual restart.
//
//   The default bind is loopback. Reach it over an SSH tunnel:
//       ssh -L 8765:127.0.0.1:8765 user@your-vps
//   To expose it instead, put nginx/Caddy with TLS in front and set
//   CCR_DASHBOARD_USER / CCR_DASHBOARD_PASSWORD below.

module.exports = {
  apps: [
    {
      name: "codecanyon-research",
      script: "serve.py",

      // Use the virtualenv's Python so beautifulsoup4 is importable.
      // Absolute path avoids surprises about pm2's working directory.
      interpreter: "/var/www/html/codecanyon-scrapper/.venv/bin/python",
      cwd: "/var/www/html/codecanyon-scrapper",

      args: "--host 127.0.0.1 --port 8765 --no-browser",

      instances: 1,          // never more: one crawler, one request rate
      exec_mode: "fork",     // it is a Python process, not a Node cluster
      autorestart: true,
      watch: false,          // see the note above
      restart_delay: 5000,
      max_restarts: 10,

      // A long crawl holds gzipped pages briefly; this is generous headroom.
      max_memory_restart: "500M",

      env: {
        PYTHONUNBUFFERED: "1",     // so pm2 logs appear as they happen
        // OPENAI_API_KEY: "sk-...",
        // CCR_DASHBOARD_USER: "you",
        // CCR_DASHBOARD_PASSWORD: "a long random string",
      },

      out_file: "logs/dashboard.out.log",
      error_file: "logs/dashboard.err.log",
      merge_logs: true,
      time: true,
    },
  ],
};
