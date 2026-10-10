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


# ---- Space invaders ---------------------------------------------------------------

class InvadersGame:
    """Court coordinates: 0..width across, 0..height down; your cannon at the bottom."""
    COLS, ROWS = 8, 5
    ALIEN_W, ALIEN_H, GAP_X, GAP_Y = 16, 10, 8, 7
    PLAYER_W = 18
    ROW_POINTS = (30, 20, 20, 10, 10)

    def __init__(self, width, height, seed=None):
        self.rng = random.Random(seed)
        self.width, self.height = width, height
        self.score, self.lives, self.wave = 0, 3, 1
        self.player = width/2
        self.new_wave()

    @property
    def player_y(self):
        return self.height - 14

    def new_wave(self):
        self.aliens = {(r, c) for r in range(self.ROWS) for c in range(self.COLS)}
        span = self.COLS*(self.ALIEN_W+self.GAP_X) - self.GAP_X
        self.offset_x = (self.width - span)/2
        self.offset_y = 16 + min(self.wave-1, 4)*8
        self.direction, self.step_timer, self.frame = 1, 0., 0
        self.bullet, self.bombs = None, []
        self.bomb_timer = 1.5
        self.shields = self.build_shields()

    def build_shields(self):
        blocks, size = set(), 4
        top = self.player_y - 36
        for i in range(4):
            centre = self.width*(i+1)/5
            for bx in range(6):
                for by in range(4):
                    if by == 3 and bx in (2, 3):
                        continue  # Notch at the bottom, like the original.
                    blocks.add((round(centre-12+bx*size), top+by*size))
        return blocks

    def resize(self, width, height):
        if (width, height) != (self.width, self.height):
            self.width, self.height = width, height
            self.player = width/2
            self.new_wave()

    def alien_rect(self, row, col):
        x = self.offset_x + col*(self.ALIEN_W+self.GAP_X)
        y = self.offset_y + row*(self.ALIEN_H+self.GAP_Y)
        return (x, y, x+self.ALIEN_W, y+self.ALIEN_H)

    def step_interval(self):
        """The formation speeds up as it thins out, and with each wave."""
        remaining = len(self.aliens) / (self.ROWS*self.COLS)
        return max(.04, (.05 + .55*remaining) * .9**(self.wave-1))

    def aim(self, across):
        half = self.PLAYER_W/2
        self.player = max(half, min(self.width-half, across))

    def fire(self):
        if self.bullet is None and not self.over:
            self.bullet = [self.player, self.player_y-6]
            return True
        return False

    @property
    def over(self):
        return self.lives <= 0

    def _shield_hit(self, x, y):
        for bx, by in self.shields:
            if bx <= x < bx+4 and by <= y < by+4:
                self.shields.discard((bx, by))
                return True
        return False

    def _march(self):
        rects = [self.alien_rect(r, c) for r, c in self.aliens]
        left, right = min(r[0] for r in rects), max(r[2] for r in rects)
        if (self.direction > 0 and right+4 > self.width-4) or (self.direction < 0 and left-4 < 4):
            self.offset_y += 8
            self.direction = -self.direction
        else:
            self.offset_x += 4*self.direction
        self.frame ^= 1

    def update(self, dt):
        """Returns 'hit' (you lost a life), 'invaded', 'wave' (cleared) or None."""
        dt = min(dt, .05)
        if self.over:
            return None
        self.step_timer += dt
        while self.aliens and self.step_timer >= self.step_interval():
            self.step_timer -= self.step_interval()
            self._march()
        if self.aliens and max(self.alien_rect(r, c)[3] for r, c in self.aliens) >= self.player_y - 6:
            self.lives = 0
            return 'invaded'
        steps = max(1, math.ceil(dt/(1/240)))
        for _ in range(steps):
            h = dt/steps
            if self.bullet is not None:
                self.bullet[1] -= 260*h
                bx, by = self.bullet
                if by < 0 or self._shield_hit(bx, by):
                    self.bullet = None
                else:
                    for row, col in sorted(self.aliens):
                        x0, y0, x1, y1 = self.alien_rect(row, col)
                        if x0 <= bx <= x1 and y0 <= by <= y1:
                            self.aliens.discard((row, col))
                            self.score += self.ROW_POINTS[row] * self.wave
                            self.bullet = None
                            break
            for bomb in list(self.bombs):
                bomb[1] += 110*h
                if bomb[1] > self.height or self._shield_hit(*bomb):
                    self.bombs.remove(bomb)
                elif (bomb[1] >= self.player_y-4 and
                      abs(bomb[0]-self.player) <= self.PLAYER_W/2):
                    self.lives -= 1
                    self.bombs.clear()
                    self.bullet = None
                    return 'hit'
        self.bomb_timer -= dt
        if self.bomb_timer <= 0 and self.aliens:
            columns = {}
            for row, col in self.aliens:
                columns[col] = max(row, columns.get(col, -1))
            col = self.rng.choice(sorted(columns))
            x0, _, x1, y1 = self.alien_rect(columns[col], col)
            self.bombs.append([(x0+x1)/2, y1])
            self.bomb_timer = self.rng.uniform(.6, 1.5) / (1 + .15*(self.wave-1))
        if not self.aliens:
            self.wave += 1
            self.new_wave()
            return 'wave'
        return None


# ---- Minesweeper ------------------------------------------------------------------

class MinesGame:
    def __init__(self, cols, rows, mines, seed=None):
        self.rng = random.Random(seed)
        self.cols, self.rows, self.mine_count = cols, rows, mines
        self.mines, self.revealed, self.flags = set(), set(), set()
        self.lost, self.exploded = False, None

    def neighbours(self, cell):
        x, y = cell
        return [(x+dx, y+dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                if (dx or dy) and 0 <= x+dx < self.cols and 0 <= y+dy < self.rows]

    def count(self, cell):
        return sum(n in self.mines for n in self.neighbours(cell))

    def _place(self, safe):
        """Mines go in after the first dig, never on or next to it."""
        keep_clear = {safe, *self.neighbours(safe)}
        cells = [(x, y) for x in range(self.cols) for y in range(self.rows) if (x, y) not in keep_clear]
        self.mines = set(self.rng.sample(cells, min(self.mine_count, len(cells))))

    @property
    def won(self):
        return not self.lost and len(self.revealed) == self.cols*self.rows - len(self.mines) and bool(self.mines)

    def reveal(self, cell):
        """Returns 'boom', 'win' or None."""
        if self.lost or cell in self.flags or cell in self.revealed:
            return None
        if not self.mines:
            self._place(cell)
        if cell in self.mines:
            self.lost, self.exploded = True, cell
            return 'boom'
        queue = [cell]
        while queue:  # Flood-fill open areas around zeros.
            current = queue.pop()
            if current in self.revealed or current in self.flags:
                continue
            self.revealed.add(current)
            if self.count(current) == 0:
                queue.extend(n for n in self.neighbours(current) if n not in self.revealed)
        return 'win' if self.won else None

    def toggle_flag(self, cell):
        if cell not in self.revealed and not self.lost:
            self.flags ^= {cell}

    def chord(self, cell):
        """Tap a number whose flags are all placed to open the rest of its neighbours."""
        if cell not in self.revealed:
            return None
        neighbours = self.neighbours(cell)
        if sum(n in self.flags for n in neighbours) != self.count(cell):
            return None
        result = None
        for n in neighbours:
            result = self.reveal(n) or result
            if result == 'boom':
                return result
        return result


# ---- Klondike solitaire -----------------------------------------------------------

SUITS = ('♠', '♥', '♦', '♣')  # spades, hearts, diamonds, clubs
RANKS = {1: 'A', 11: 'J', 12: 'Q', 13: 'K'}


def red(card):
    return card[1] in (1, 2)


def rank_name(card):
    return RANKS.get(card[0], str(card[0]))


class Klondike:
    """Draw-one Klondike. Cards are (rank 1-13, suit 0-3); tableau entries [card, face_up]."""
    def __init__(self, seed=None):
        rng = random.Random(seed)
        deck = [(rank, suit) for suit in range(4) for rank in range(1, 14)]
        rng.shuffle(deck)
        self.tableau = []
        for pile in range(7):
            cards = [[deck.pop(), False] for _ in range(pile+1)]
            cards[-1][1] = True
            self.tableau.append(cards)
        self.stock, self.waste = deck, []
        self.foundations = [[] for _ in range(4)]  # Indexed by suit.
        self.moves = 0

    @property
    def won(self):
        return all(len(f) == 13 for f in self.foundations)

    def draw(self):
        if self.stock:
            self.waste.append(self.stock.pop())
        elif self.waste:
            self.stock = self.waste[::-1]
            self.waste = []
        else:
            return False
        self.moves += 1
        return True

    def cards_at(self, source):
        kind = source[0]
        if kind == 'waste':
            return self.waste[-1:]
        if kind == 'found':
            return self.foundations[source[1]][-1:]
        pile, index = source[1], source[2]
        stack = self.tableau[pile][index:]
        return [card for card, up in stack] if stack and all(up for _, up in stack) else []

    def can_found(self, card):
        pile = self.foundations[card[1]]
        return card[0] == (pile[-1][0]+1 if pile else 1)

    def can_stack(self, card, pile):
        target = self.tableau[pile]
        if not target:
            return card[0] == 13
        top, up = target[-1]
        return up and red(top) != red(card) and top[0] == card[0]+1

    def move(self, source, destination):
        cards = self.cards_at(source)
        if not cards:
            return False
        if destination[0] == 'found':
            if len(cards) != 1 or not self.can_found(cards[0]):
                return False
            self.foundations[cards[0][1]].append(cards[0])
        elif destination[0] == 'tab':
            if source[:2] == destination[:2] or not self.can_stack(cards[0], destination[1]):
                return False
            self.tableau[destination[1]].extend([card, True] for card in cards)
        else:
            return False
        self._remove(source, len(cards))
        self.moves += 1
        return True

    def _remove(self, source, count):
        if source[0] == 'waste':
            self.waste.pop()
        elif source[0] == 'found':
            self.foundations[source[1]].pop()
        else:
            pile = self.tableau[source[1]]
            del pile[len(pile)-count:]
            if pile and not pile[-1][1]:
                pile[-1][1] = True  # Turn over the newly exposed card.

    def auto_move(self, source):
        """Foundation first (single cards), then the first tableau pile that accepts it."""
        if len(self.cards_at(source)) == 1 and self.move(source, ('found',)):
            return True
        return any(self.move(source, ('tab', pile)) for pile in range(7))


# ---- Sudoku -----------------------------------------------------------------------

def _candidates(grid, index):
    row, col = divmod(index, 9)
    used = set(grid[row*9:row*9+9]) | {grid[r*9+col] for r in range(9)}
    br, bc = row//3*3, col//3*3
    used |= {grid[(br+r)*9+bc+c] for r in range(3) for c in range(3)}
    return [n for n in range(1, 10) if n not in used]


def sudoku_solutions(grid, limit=2, rng=None, out=None):
    """Count solutions (stopping at `limit`), filling `out` with the first one found.
    Picks the emptiest-choice cell first, which keeps it fast enough for the Pi."""
    grid = list(grid)
    found = [0]

    def search():
        best, options = None, None
        for i, value in enumerate(grid):
            if value == 0:
                choices = _candidates(grid, i)
                if not choices:
                    return False
                if options is None or len(choices) < len(options):
                    best, options = i, choices
                    if len(options) == 1:
                        break
        if best is None:
            found[0] += 1
            if out is not None and found[0] == 1:
                out[:] = grid
            return found[0] >= limit
        if rng is not None:
            rng.shuffle(options)
        for value in options:
            grid[best] = value
            if search():
                return True
        grid[best] = 0
        return False

    search()
    return found[0]


def make_sudoku(seed=None, clues=34):
    """A puzzle with exactly one solution. Returns (puzzle, solution) as 81-item lists."""
    rng = random.Random(seed)
    solution = [0]*81
    sudoku_solutions([0]*81, limit=1, rng=rng, out=solution)
    puzzle = list(solution)
    cells = list(range(81))
    rng.shuffle(cells)
    for index in cells:
        if sum(1 for v in puzzle if v) <= clues:
            break
        value, puzzle[index] = puzzle[index], 0
        if sudoku_solutions(puzzle, limit=2) != 1:
            puzzle[index] = value  # Removing it would allow a second answer.
    return puzzle, solution


class SudokuGame:
    def __init__(self, seed=None, clues=34):
        self.puzzle, self.solution = make_sudoku(seed, clues)
        self.entries = list(self.puzzle)
        self.selected = None
        self.mistakes = 0

    def given(self, index):
        return self.puzzle[index] != 0

    def place(self, value):
        """Put 1-9 (or 0 to erase) in the selected cell. Returns 'win', 'wrong' or None."""
        index = self.selected
        if index is None or self.given(index):
            return None
        self.entries[index] = value
        if value and value != self.solution[index]:
            self.mistakes += 1
            return 'wrong'
        return 'win' if self.won else None

    def wrong(self, index):
        return bool(self.entries[index]) and self.entries[index] != self.solution[index]

    @property
    def won(self):
        return self.entries == self.solution
