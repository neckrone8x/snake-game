# Snake Pro X

A modern take on the classic Snake game, with power-ups, enemies, combos, four difficulty levels, a global leaderboard and accounts.

## Play

**Play online:** https://neckrone8x.github.io/snake-game/

**Quick play (no install):** open `index.html` (or `snake_pro_v2/templates/index.html`) in any browser. The game works offline.

**GitHub Pages / static version:** plays solo. The page checks for the Flask server when it loads; without it, the multiplayer button, leaderboard and account controls are hidden, so there are no broken buttons.

- The global leaderboard and accounts need the Flask server (below).
- Multiplayer is a work in progress.

**Full version (leaderboard, accounts):**

```bash
cd snake_pro_v2
python -m venv .venv
.venv\Scripts\activate        # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000. In production, set the `SECRET_KEY` environment variable.

## How to play

- Pick a difficulty: Easy, Normal, Hard or Insane.
- Steer the snake to eat food. Golden food is worth 5 points, and eating quickly builds a combo multiplier.
- Avoid walls, your own tail and the red enemies.
- Collect power-ups: ⚡ Speed, 🐢 Slow, 🛡 Shield, 👻 Ghost (pass through walls), ✨ Double points, 🧲 Magnet, ❤️ Extra life.

| Action  | Keyboard            | Phone                   |
|---------|---------------------|-------------------------|
| Move    | Arrow keys / WASD   | Swipe on board or D-pad |
| Pause   | Space / P           | ⏸ Pause button          |
| Restart | R                   | ↻ Restart button        |

## Built with

- HTML, CSS and JavaScript (canvas game, single page)
- Python + Flask and Flask-SocketIO (optional server for scores and accounts)
- SQLite
