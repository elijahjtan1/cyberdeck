"""Game rules for Tetris and Pong. No drawing or hardware: the panel app renders them."""
import math
import random

# ---- Tetris -----------------------------------------------------------------------

# Each piece: list of rotation states, each a tuple of (col, row) cells in a 4x4 box.
def _rotations(cells, count):
    states, current = [], cells
    for _ in range(count):
        states.append(tuple(sorted(current)))
        # Rotate clockwise inside the box: (x, y) -> (size-1-y, x).
        size = 4 if max(max(x, y) for x, y in cells) == 3 else 3
        current = [(size-1-y, x) for x, y in current]
    return states


PIECES = {
    'I': _rotations([(0, 1), (1, 1), (2, 1), (3, 1)], 4),
    'O': [((1, 0), (1, 1), (2, 0), (2, 1))],
    'T': _rotations([(1, 0), (0, 1), (1, 1), (2, 1)], 4),
    'S': _rotations([(1, 0), (2, 0), (0, 1), (1, 1)], 4),
    'Z': _rotations([(0, 0), (1, 0), (1, 1), (2, 1)], 4),
    'J': _rotations([(0, 0), (0, 1), (1, 1), (2, 1)], 4),
    'L': _rotations([(2, 0), (0, 1), (1, 1), (2, 1)], 4),
}
PIECE_COLOURS = {'I': '#4fd8e8', 'O': '#f2d24b', 'T': '#b07cf0', 'S': '#5fd36b',
                 'Z': '#f0605a', 'J': '#4f86f0', 'L': '#f29b42'}
LINE_SCORES = {1: 100, 2: 300, 3: 500, 4: 800}
KICKS = (0, -1, 1, -2, 2)  # Simple wall kicks: try nudging sideways when a rotation is blocked.


class TetrisGame:
    COLS, ROWS = 10, 20

    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.reset()

    def reset(self):
        self.board = [[None]*self.COLS for _ in range(self.ROWS)]
        self.bag = []
        self.score = self.lines = 0
        self.alive = True
        self.next_kind = self._draw()
        self.spawn()

    @property
    def level(self):
        return self.lines // 10 + 1

    def gravity(self):
        """Seconds per row: 0.8 s at level 1, speeding up to 0.1 s."""
        return max(.1, .8 - (self.level-1)*.07)

    def _draw(self):
        if not self.bag:  # 7-bag: every piece once per seven, so no long droughts.
            self.bag = list(PIECES)
            self.rng.shuffle(self.bag)
        return self.bag.pop()

    def spawn(self):
        self.kind, self.next_kind = self.next_kind, self._draw()
        self.rotation = 0
        self.x, self.y = 3, 0
        if not self.fits(self.cells()):
            self.alive = False

    def cells(self, x=None, y=None, rotation=None):
        states = PIECES[self.kind]
        rotation = self.rotation if rotation is None else rotation
        x = self.x if x is None else x
        y = self.y if y is None else y
        return [(x+cx, y+cy) for cx, cy in states[rotation % len(states)]]

    def fits(self, cells):
        return all(0 <= x < self.COLS and y < self.ROWS and (y < 0 or self.board[y][x] is None)
                   for x, y in cells)

    def move(self, dx):
        if self.alive and self.fits(self.cells(x=self.x+dx)):
            self.x += dx
            return True
        return False

    def rotate(self):
        if not self.alive:
            return False
        rotation = self.rotation + 1
        for kick in KICKS:
            if self.fits(self.cells(x=self.x+kick, rotation=rotation)):
                self.x, self.rotation = self.x+kick, rotation % len(PIECES[self.kind])
                return True
        return False

    def step(self, soft=False):
        """Move down one row; lock the piece if it cannot. Returns True if it moved."""
        if not self.alive:
            return False
        if self.fits(self.cells(y=self.y+1)):
            self.y += 1
            if soft:
                self.score += 1
            return True
        self.lock()
        return False

    def hard_drop(self):
        rows = 0
        while self.alive and self.fits(self.cells(y=self.y+1)):
            self.y += 1
            rows += 1
        self.score += 2*rows
        self.lock()
        return rows

    def ghost_y(self):
        y = self.y
        while self.fits(self.cells(y=y+1)):
            y += 1
        return y

    def lock(self):
        for x, y in self.cells():
            if y < 0:
                self.alive = False
                return
            self.board[y][x] = self.kind
        full = [row for row in range(self.ROWS) if all(self.board[row])]
        if full:
            level = self.level
            for row in full:
                del self.board[row]
                self.board.insert(0, [None]*self.COLS)
            self.lines += len(full)
            self.score += LINE_SCORES[len(full)] * level
        self.spawn()


# ---- Pong -------------------------------------------------------------------------

class PongGame:
    """One player against the computer. Coordinates are in a canonical court:
    `across` runs side to side (0..width), `along` runs from the player's paddle (0)
    to the computer's (length). The app maps this onto the screen for each rotation."""
    PADDLE, THICK, BALL = 46, 6, 6
    TARGET = 7  # Points to win.

    def __init__(self, width, length, seed=None):
        self.rng = random.Random(seed)
        self.width, self.length = width, length
        self.player_score = self.cpu_score = 0
        self.player = self.cpu = width/2
        self.serve(toward_player=self.rng.random() < .5)

    def resize(self, width, length):
        if (width, length) != (self.width, self.length):
            self.width, self.length = width, length
            self.player = self.cpu = width/2
            self.serve(toward_player=True)

    def serve(self, toward_player):
        self.ball = [self.width/2, self.length/2]
        angle = self.rng.uniform(-.5, .5)
        self.speed = 150.
        direction = -1 if toward_player else 1
        self.velocity = [self.speed*math.sin(angle), direction*self.speed*math.cos(angle)]
        self.wait = .8  # Short pause before each serve.

    @property
    def over(self):
        return max(self.player_score, self.cpu_score) >= self.TARGET

    def aim(self, across):
        """Player paddle follows the finger, kept inside the court."""
        half = self.PADDLE/2
        self.player = max(half, min(self.width-half, across))

    def _cpu_move(self, dt):
        # Track the ball when it is coming; otherwise drift to the centre. Capped speed and a
        # small aiming error keep it beatable.
        target = self.ball[0] if self.velocity[1] > 0 else self.width/2
        target += 10*math.sin(self.ball[1]/37)
        limit = (130 + 12*self.cpu_score) * dt
        self.cpu += max(-limit, min(limit, target-self.cpu))
        half = self.PADDLE/2
        self.cpu = max(half, min(self.width-half, self.cpu))

    def _bounce(self, paddle, toward):
        offset = (self.ball[0]-paddle) / (self.PADDLE/2)  # -1 .. 1 along the paddle
        self.speed = min(420., self.speed*1.07)
        angle = max(-1., min(1., offset)) * 1.0  # Up to ~57 degrees off straight.
        self.velocity = [self.speed*math.sin(angle), toward*self.speed*math.cos(angle)]

    def update(self, dt):
        """Advance the game. Returns 'player', 'cpu' when a point is scored, else None."""
        dt = min(dt, .05)
        if self.over:
            return None
        if self.wait > 0:
            self.wait -= dt
            self._cpu_move(dt)
            return None
        steps = max(1, math.ceil(dt/(1/240)))  # Small steps so a fast ball never skips a paddle.
        for _ in range(steps):
            h = dt/steps
            self._cpu_move(h)
            self.ball[0] += self.velocity[0]*h
            self.ball[1] += self.velocity[1]*h
            r = self.BALL/2
            if self.ball[0] < r or self.ball[0] > self.width-r:
                self.ball[0] = max(r, min(self.width-r, self.ball[0]))
                self.velocity[0] = -self.velocity[0]
            near, far = self.THICK + r, self.length - self.THICK - r
            if self.velocity[1] < 0 and self.ball[1] <= near:
                if abs(self.ball[0]-self.player) <= self.PADDLE/2 + r:
                    self.ball[1] = near
                    self._bounce(self.player, 1)
                elif self.ball[1] < 0:
                    self.cpu_score += 1
                    self.serve(toward_player=True)
                    return 'cpu'
            elif self.velocity[1] > 0 and self.ball[1] >= far:
                if abs(self.ball[0]-self.cpu) <= self.PADDLE/2 + r:
                    self.ball[1] = far
                    self._bounce(self.cpu, -1)
                elif self.ball[1] > self.length:
                    self.player_score += 1
                    self.serve(toward_player=False)
                    return 'player'
        return None
