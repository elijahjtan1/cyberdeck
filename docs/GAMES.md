# Games: Tetris, Pong, Brick Breaker and Flappy

![Games in landscape](previews/games-landscape-preview.png)

![Games in portrait](previews/games-portrait-preview.png)

*The previews are drawn by the app's own code with sample game states.*

The Apps page's **GAMES** tile opens a two-column menu: **Snake**, **Tetris**, **Pong**, **Bricks** (Brick Breaker) and **Flappy**. Each shows your record. **BACK** returns to Apps. Quitting a game returns to the Games menu.

All three games work in every screen rotation. While a game is running, the screensaver won't start.

## Tetris

The standard 10×20 board with all seven pieces. Pieces are dealt from a shuffled "bag" of seven, so you never wait long for a long bar.

| Control | What it does |
| --- | --- |
| **< LEFT / RIGHT >** | Move one column. Hold the button to keep sliding: repeats begin after about 0.3 s. |
| **ROTATE**, or tap the board | Rotate clockwise. Next to a wall or other blocks, it nudges the piece sideways when it can. |
| **DOWN** | Move down one row (+1 point). Hold it to keep going. |
| **DROP** | Drop straight to the bottom (+2 points per row). |
| **PAUSE** | Opens Resume / Quit. |

- **Ghost outline:** shows where the piece will land.
- **NEXT:** shows the piece coming after it.
- **Scoring:** clearing 1, 2, 3 or 4 lines scores 100, 300, 500 or 800 points, multiplied by the level.
- **Levels:** the level goes up every 10 lines, and pieces fall faster each level: 0.8 s per row at level 1, down to 0.1 s.
- **Game over:** when a new piece has no room to appear.
- **Layout:** in landscape the controls sit to the right of the board; in portrait they're in two rows underneath.
- **Paused games:** time spent paused, or with the app busy, never causes a burst of catch-up moves when you resume.

## Pong

You play against the computer, and the first to 7 points wins.

- **Moving your paddle:** keep a finger on the screen and drag. Your paddle follows it anywhere on the court. In landscape your paddle is on the **left**; in portrait it's at the **bottom**.
- **Angles:** where the ball hits your paddle decides its angle. The edges send it off steeply.
- **Speed:** the ball speeds up a little with every hit.
- **The computer:** it's beatable. Its paddle has a speed limit and aims slightly off, and it gets a little quicker each time it scores.
- **Serves:** after each point there's a short pause before the next serve.
- **PAUSE** is in the top right.

Dragging needed one small addition to the touch code: the app can now see where a finger is while it's still down. Before, it only learned about a tap when the finger lifted. Everything else still acts on release, as before.

## Brick Breaker

Clear the wall of 48 bricks with the ball. You have 3 lives (the hearts in the header).

- **Paddle:** drag a finger anywhere on the court and the paddle follows it. The paddle is at the bottom in every rotation.
- **Launch:** the ball waits on the paddle; tap the court to launch it, including after losing a life.
- **Angles:** where the ball hits the paddle decides its angle, as in Pong.
- **Speed:** the ball speeds up slightly with each paddle hit.
- **Scoring:** top rows are worth more (50, 40, 30, 20, 10, 10), multiplied by the level.
- **Levels:** clearing the wall starts the next level with a fresh wall and a faster ball.

## Flappy

Fly between the pipes, scoring a point for each one you pass. Hitting a pipe, the ground or the top ends the run.

- **Flap:** touch anywhere. It flaps the moment your finger touches the screen, not when you lift it, so it reacts immediately. Keeping your finger down doesn't keep flapping; touch again for the next flap.
- **Gap size:** the gap scales with the screen height, so it's a little roomier in portrait.

## Records

`~/cyberdeck-data/games.json` now holds:

- `snake_best`
- `tetris_best`
- `pong_wins`
- `bricks_best`
- `flappy_best`

Saving one game's record keeps the others. Your existing Snake best score carries over.

## Checks

All 91 automated tests pass (`python3 -m unittest discover -s tests -v`), including 24 in `tests/test_games.py`:

- **Tetris rules:**
  - every piece has 4 cells in every rotation
  - the 7-bag deals each piece once
  - clearing lines scores correctly and the rows above drop down
  - 4-line clears and faster levels work
  - pieces stop at walls, and rotation nudges away from them
  - the game ends when a new piece can't appear
- **Tetris controls, in landscape and portrait:**
  - every button is on screen
  - a tap moves one column
  - holding repeats, and lifting the finger doesn't add an extra move
  - tapping the board rotates
  - the best score is saved only when it's beaten
  - a long gap doesn't cause catch-up moves
- **Pong rules:**
  - side-wall and paddle bounces
  - missed balls score
  - a fast ball can't pass through a paddle
  - first to 7 ends the game
  - the computer's speed limit
  - the paddle stays inside the court
- **Pong controls:**
  - dragging puts your paddle under your finger in both orientations
  - the header is ignored for dragging
  - wins are saved
  - pausing works
  - the court resizes after a screen rotation
- **Brick Breaker rules:**
  - the ball rides on the paddle until launched
  - hitting a brick removes it, scores, and bounces the ball back
  - the paddle returns the ball
  - a missed ball costs a life, and three misses end the game
  - clearing the wall starts a faster level with a full wall
- **Flappy rules:**
  - gravity and flapping
  - a point for each pipe passed
  - crashes into pipes and the ground
  - pipes keep coming, with the gap always fully on screen
- **Brick Breaker and Flappy controls, in landscape and portrait:**
  - the paddle follows a drag
  - a tap launches the ball
  - Flappy flaps on press, ignores the release, and doesn't flap on PAUSE
  - records are saved
- **Games menu:** all five buttons and BACK lead to the right place in both orientations.
- **Real application loop** with a simulated screen, touch and camera:
  1. Apps → Games → Tetris → Start → Drop → Pause → Quit
  2. Pong → Start → Pause → Quit
  3. Bricks → Start → launch → Pause → Quit
  4. Flappy → Start → Pause → Quit
  5. Back → Exit
- **Updated Snake test:** the end-to-end Snake test now goes through the Games menu.

**Still needs checking on the Pi:**

- how responsive Tetris's buttons and hold-to-repeat feel on the resistive screen
- how smoothly Pong's paddle tracks your finger
- whether the computer opponent is fun to play against: easy to adjust if it's too easy or too hard
- whether Flappy's flap and gravity feel right on the 2.8" screen: easy to tune
