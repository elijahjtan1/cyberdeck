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


# ---- Brick breaker ----------------------------------------------------------------

class BrickGame:
    """Breakout in court coordinates (0..width across, 0..height down; paddle at the bottom)."""
    COLS, ROWS = 8, 6
    PADDLE, THICK, BALL = 52, 6, 6
    ROW_POINTS = (50, 40, 30, 20, 10, 10)  # Top rows are worth more.

    def __init__(self, width, height, seed=None):
        self.rng = random.Random(seed)
        self.width, self.height = width, height
        self.score, self.lives, self.level = 0, 3, 1
        self.paddle = width/2
        self.new_level()

    def new_level(self):
        self.bricks = {(row, col) for row in range(self.ROWS) for col in range(self.COLS)}
        self.reset_ball()

    def reset_ball(self):
        self.stuck = True  # Ball rests on the paddle until launched.
        self.speed = 160. + 20*(self.level-1)
        self.ball = [self.paddle, self.paddle_y() - self.BALL]
        self.velocity = [0., 0.]

    def resize(self, width, height):
        if (width, height) != (self.width, self.height):
            self.width, self.height = width, height
            self.paddle = width/2
            self.reset_ball()

    def paddle_y(self):
        return self.height - 12

    def brick_rect(self, row, col):
        margin, gap, top = 6, 3, 18
        width = (self.width - 2*margin - (self.COLS-1)*gap) / self.COLS
        x0 = margin + col*(width+gap)
        y0 = top + row*(10+gap)
        return (x0, y0, x0+width, y0+10)

    def aim(self, across):
        half = self.PADDLE/2
        self.paddle = max(half, min(self.width-half, across))
        if self.stuck:
            self.ball[0] = self.paddle

    def launch(self):
        if self.stuck and self.lives > 0:
            angle = self.rng.uniform(-.5, .5)
            self.velocity = [self.speed*math.sin(angle), -self.speed*math.cos(angle)]
            self.stuck = False

    @property
    def over(self):
        return self.lives <= 0

    def _hit_brick(self):
        r = self.BALL/2
        bx, by = self.ball
        for row, col in sorted(self.bricks):
            x0, y0, x1, y1 = self.brick_rect(row, col)
            if x0-r <= bx <= x1+r and y0-r <= by <= y1+r:
                self.bricks.discard((row, col))
                self.score += self.ROW_POINTS[row] * self.level
                return True
        return False

    def update(self, dt):
        """Advance; returns 'lost' when the ball is missed, 'cleared' on a new level."""
        dt = min(dt, .05)
        if self.stuck or self.over:
            return None
        steps = max(1, math.ceil(dt/(1/240)))
        r = self.BALL/2
        for _ in range(steps):
            h = dt/steps
            # Move one axis at a time so a brick hit flips the right component.
            self.ball[0] += self.velocity[0]*h
            if self.ball[0] < r or self.ball[0] > self.width-r:
                self.ball[0] = max(r, min(self.width-r, self.ball[0]))
                self.velocity[0] = -self.velocity[0]
            elif self._hit_brick():
                self.velocity[0] = -self.velocity[0]
            self.ball[1] += self.velocity[1]*h
            if self.ball[1] < r:
                self.ball[1], self.velocity[1] = r, abs(self.velocity[1])
            elif self._hit_brick():
                self.velocity[1] = -self.velocity[1]
            top = self.paddle_y()
            if (self.velocity[1] > 0 and top-r <= self.ball[1] <= top+self.THICK
                    and abs(self.ball[0]-self.paddle) <= self.PADDLE/2 + r):
                offset = max(-1., min(1., (self.ball[0]-self.paddle)/(self.PADDLE/2)))
                self.speed = min(380., self.speed*1.02)
                self.velocity = [self.speed*math.sin(offset), -self.speed*math.cos(offset)]
                self.ball[1] = top - r
            if self.ball[1] > self.height + r:
                self.lives -= 1
                if self.lives > 0:
                    self.reset_ball()
                return 'lost'
            if not self.bricks:
                self.level += 1
                self.new_level()
                return 'cleared'
        return None


# ---- Flappy bird ------------------------------------------------------------------

class FlappyGame:
    """Fly between pipes. Court coordinates: 0..width across, 0..height down."""
    GRAVITY, FLAP, SPEED = 620., -215., 85.
    RADIUS, PIPE = 6, 30

    def __init__(self, width, height, seed=None):
        self.rng = random.Random(seed)
        self.width, self.height = width, height
        self.reset()

    def reset(self):
        self.x = self.width * .28
        self.y, self.vy = self.height/2, 0.
        self.score, self.alive = 0, True
        self.gap = max(62, min(84, self.height*.3))
        self.pipes = []  # [x, gap_centre, passed]
        self.next_pipe = self.width*.55

    def resize(self, width, height):
        if (width, height) != (self.width, self.height):
            self.width, self.height = width, height
            self.reset()

    def flap(self):
        if self.alive:
            self.vy = self.FLAP

    def _spawn(self):
        margin = self.gap/2 + 12
        self.pipes.append([self.width + self.PIPE, self.rng.uniform(margin, self.height-margin), False])

    def update(self, dt):
        """Returns 'point' when a pipe is passed, 'crash' when the bird hits something."""
        dt = min(dt, .05)
        if not self.alive:
            return None
        self.vy = min(self.vy + self.GRAVITY*dt, 400.)
        self.y += self.vy*dt
        event = None
        self.next_pipe -= self.SPEED*dt
        if self.next_pipe <= 0:
            self._spawn()
            self.next_pipe = max(120., self.width*.5)
        for pipe in self.pipes:
            pipe[0] -= self.SPEED*dt
            if not pipe[2] and pipe[0] + self.PIPE/2 < self.x:
                pipe[2] = True
                self.score += 1
                event = 'point'
        self.pipes = [p for p in self.pipes if p[0] > -self.PIPE]
        r = self.RADIUS
        if self.y - r < 0 or self.y + r > self.height:
            self.alive = False
            return 'crash'
        for x, centre, _ in self.pipes:
            if abs(self.x - x) < self.PIPE/2 + r and abs(self.y - centre) > self.gap/2 - r:
                self.alive = False
                return 'crash'
        return event
