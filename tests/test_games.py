"""Tetris and Pong: rules, touch controls in both orientations, and the app loop."""
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import panel_app as app  # noqa: E402
from panel_tools.games import PIECES, TetrisGame, PongGame, BrickGame, FlappyGame  # noqa: E402


def centre(rect):
    return (rect[0]+rect[2])/2, (rect[1]+rect[3])/2


class TetrisRulesTest(unittest.TestCase):
    def test_every_rotation_has_four_cells_in_its_box(self):
        for kind, states in PIECES.items():
            for cells in states:
                self.assertEqual(len(set(cells)), 4, kind)
                self.assertTrue(all(0 <= x < 4 and 0 <= y < 4 for x, y in cells), kind)
        self.assertEqual(len(PIECES['I']), 4)
        self.assertEqual(len(PIECES['O']), 1)

    def test_seven_bag_deals_each_piece_once(self):
        game = TetrisGame(seed=3)
        dealt = [game.kind, game.next_kind]
        for _ in range(12):
            game.spawn()
            dealt.append(game.next_kind)
        self.assertEqual(sorted(dealt[:7]), sorted(PIECES))
        self.assertEqual(sorted(dealt[7:14]), sorted(PIECES))

    def test_line_clear_scores_by_level_and_drops_rows(self):
        game = TetrisGame(seed=1)
        game.board[19] = ['Z']*9 + [None]
        game.board[18][0] = 'S'
        game.kind, game.rotation, game.x, game.y = 'I', 1, 7, 0  # Vertical I in column 9.
        rows = game.hard_drop()
        self.assertEqual(game.lines, 1)
        self.assertEqual(game.score, 2*rows + 100)
        self.assertEqual(game.board[19][0], 'S')  # The row above fell into place.
        self.assertEqual(game.board[19][9], 'I')

    def test_tetris_bonus_and_level_speed(self):
        game = TetrisGame(seed=2)
        game.lines = 10
        self.assertEqual(game.level, 2)
        self.assertLess(game.gravity(), TetrisGame(seed=2).gravity())
        for row in range(16, 20):
            game.board[row] = ['J']*9 + [None]
        game.kind, game.rotation, game.x, game.y = 'I', 1, 7, 0
        game.hard_drop()
        self.assertEqual(game.lines, 14)
        self.assertGreaterEqual(game.score, 800*2)

    def test_moves_stop_at_walls_and_rotation_kicks(self):
        game = TetrisGame(seed=4)
        game.kind, game.rotation, game.x, game.y = 'I', 1, 7, 5  # Vertical against the right.
        while game.move(1):
            pass
        self.assertEqual(max(x for x, _ in game.cells()), 9)
        self.assertTrue(game.rotate())  # Kicked left instead of failing.
        self.assertTrue(all(0 <= x < 10 for x, _ in game.cells()))

    def test_game_over_when_spawn_is_blocked(self):
        game = TetrisGame(seed=5)
        for row in range(0, 2):
            game.board[row] = ['T']*10
        game.spawn()
        self.assertFalse(game.alive)


class PongRulesTest(unittest.TestCase):
    def test_walls_paddles_and_scoring(self):
        game = PongGame(200, 300, seed=1)
        game.wait = 0
        game.ball, game.velocity = [5, 150], [-200, 0.1]
        game.update(.05)
        self.assertGreater(game.velocity[0], 0)  # Bounced off the side wall.
        game.ball, game.velocity = [100, 20], [0, -300]
        game.player = 100
        game.update(.05)
        self.assertGreater(game.velocity[1], 0)  # Returned by the player's paddle.
        self.assertGreater(game.speed, 150)
        game.ball, game.velocity = [10, 20], [0, -300]
        game.player = 180
        self.assertIn('cpu', (game.update(.05), game.update(.05)))  # Missed: leaves the court.
        self.assertEqual(game.cpu_score, 1)

    def test_fast_ball_cannot_tunnel_through_a_paddle(self):
        game = PongGame(200, 300, seed=2)
        game.wait, game.player = 0, 100
        game.ball, game.velocity = [100, 30], [0, -420]
        game.update(.05)
        self.assertGreater(game.velocity[1], 0)

    def test_player_can_win_and_cpu_is_capped(self):
        game = PongGame(200, 300, seed=3)
        game.wait, game.player_score = 0, 6
        game.cpu = 20
        game.ball, game.velocity = [190, 280], [0, 400]
        self.assertEqual(game.update(.05), 'player')
        self.assertTrue(game.over)
        self.assertIsNone(game.update(.05))  # Frozen once over.
        game = PongGame(200, 300, seed=4)
        start = game.cpu
        game.wait, game.ball, game.velocity = 0, [0, 200], [0, 100]
        game.update(.05)
        self.assertLessEqual(abs(game.cpu-start), 130*.05 + 1e-6)

    def test_aim_is_clamped_to_the_court(self):
        game = PongGame(200, 300)
        game.aim(-50)
        self.assertEqual(game.player, game.PADDLE/2)
        game.aim(999)
        self.assertEqual(game.player, 200-game.PADDLE/2)


class GameAppsTest(unittest.TestCase):
    def tearDown(self):
        app.set_rotation(0)

    def test_tetris_buttons_repeat_and_save_best(self):
        for rotation in (0, 90):
            app.set_rotation(rotation)
            saved = []
            tetris = app.TetrisApp(save_best=saved.append, seed=1)
            box, start, quit_ = app.SnakeApp.dialog_rects()
            tetris.tap(*centre(start), 0)
            self.assertTrue(tetris.running)
            buttons = {action: rect for rect, action, _ in app.tetris_layout()[4]}
            self.assertEqual(set(buttons), {'left', 'right', 'rotate', 'down', 'drop', 'pause'})
            for rect in buttons.values():  # Every control is on screen.
                self.assertTrue(0 <= rect[0] < rect[2] < app.W and 0 <= rect[1] < rect[3] < app.H)
            x = tetris.game.x
            tetris.tap(*centre(buttons['left']), .1)
            self.assertEqual(tetris.game.x, x-1)
            # Hold RIGHT: nothing for 0.28 s, then repeats; the release is not an extra move.
            x = tetris.game.x
            point = centre(buttons['right'])
            for t in (.2, .3, .5, .6, .7):
                tetris.hold(point, t)
            moved = tetris.game.x - x
            self.assertGreaterEqual(moved, 2)
            tetris.tap(*point, .75)
            tetris.hold(None, .8)
            self.assertEqual(tetris.game.x - x, moved)
            board = app.tetris_layout()[2]
            rotation_before = tetris.game.rotation
            tetris.tap(*centre(board), .9)  # Board tap rotates.
            self.assertNotEqual((tetris.game.rotation, tetris.game.x), (rotation_before, None))
            # Block the spawn area (rows with a gap, so they are not cleared as lines).
            tetris.game.board[0] = [None] + ['Z']*9
            tetris.game.board[1] = [None] + ['Z']*9
            tetris.game.score = 50
            tetris.tap(*centre(buttons['drop']), 1)
            self.assertEqual(tetris.state, 'over')
            self.assertEqual(saved, [tetris.game.score])
            self.assertEqual(tetris.render(1, datetime.now(), ()).size, (app.W, app.H))
            self.assertEqual(tetris.tap(*centre(quit_), 2), 'exit')

    def test_tetris_gravity_without_catch_up_bursts(self):
        tetris = app.TetrisApp(seed=2)
        tetris.tap(*centre(app.SnakeApp.dialog_rects()[1]), 0)
        y = tetris.game.y
        tetris.update(.85)
        self.assertEqual(tetris.game.y, y+1)
        tetris.update(60)  # Long gap (e.g. screensaver) - at most one row, not 70.
        self.assertLessEqual(tetris.game.y, y+3)

    def test_pong_drag_follows_finger_in_both_orientations(self):
        for rotation, point, expect in ((0, (100, 26+150), 150), (90, (60, 200), 60)):
            app.set_rotation(rotation)
            wins = []
            pong = app.PongApp(save_wins=wins.append, seed=1)
            pong.tap(*centre(app.SnakeApp.dialog_rects()[1]), 0)
            self.assertTrue(pong.running)
            pong.hold(point, .1)
            self.assertAlmostEqual(pong.game.player, expect)
            pong.hold((point[0], 10), .2)  # Header: ignored.
            self.assertAlmostEqual(pong.game.player, expect)
            # Player's paddle is drawn where the finger is.
            image = pong.render(.2, datetime.now(), ())
            x, y = app.pong_to_screen(expect, 2)
            self.assertEqual(image.getpixel((round(x), round(y))),
                             app.Image.new('RGB', (1, 1), app.ACCENT).getpixel((0, 0)))
            pong.game.player_score = 6
            pong.game.wait, pong.game.cpu = 0, 20
            pong.game.ball = [pong.game.width-5, pong.game.length-15]
            pong.game.velocity = [0, 400]
            pong.render(.25, datetime.now(), ())
            self.assertEqual(pong.state, 'over')
            self.assertEqual(wins, [1])
            pong.tap(*centre(app.SnakeApp.dialog_rects()[1]), 1)  # Play again.
            self.assertEqual((pong.game.player_score, pong.game.cpu_score), (0, 0))

    def test_pong_pause_and_resize(self):
        pong = app.PongApp(seed=2)
        pong.tap(*centre(app.SnakeApp.dialog_rects()[1]), 0)
        pong.tap(app.W-20, 10, .1)
        self.assertEqual(pong.state, 'paused')
        app.set_rotation(90)
        pong.render(.2, datetime.now(), ())
        self.assertEqual((pong.game.width, pong.game.length), (240, 320-26))

    def test_games_menu(self):
        for rotation in (0, 90):
            app.set_rotation(rotation)
            menu = app.GamesMenu(lambda: {'tetris': 'Best score: 9'})
            games, back = menu.rects()
            self.assertEqual([menu.tap(*centre(r), 0) for r in games],
                             ['snake', 'tetris', 'pong', 'bricks', 'flappy'])
            self.assertEqual(menu.tap(*centre(back), 0), 'exit')
            self.assertEqual(menu.render(0, datetime.now(), ()).size, (app.W, app.H))


class BrickRulesTest(unittest.TestCase):
    def test_launch_bricks_paddle_and_lives(self):
        game = BrickGame(300, 200, seed=1)
        self.assertTrue(game.stuck)
        game.aim(40)
        self.assertEqual(game.ball[0], 40)  # The resting ball rides on the paddle.
        game.launch()
        self.assertFalse(game.stuck)
        self.assertLess(game.velocity[1], 0)
        bricks = len(game.bricks)
        x0, y0, x1, y1 = game.brick_rect(5, 3)
        game.ball, game.velocity = [(x0+x1)/2, y1+6], [0, -200]
        game.update(.05)
        self.assertEqual(len(game.bricks), bricks-1)
        self.assertEqual(game.score, 10)
        self.assertGreater(game.velocity[1], 0)  # Bounced back down.
        game.paddle = 150
        game.ball, game.velocity = [150, game.paddle_y()-5], [0, 200]
        game.update(.02)
        self.assertLess(game.velocity[1], 0)  # Paddle return.
        game.ball, game.velocity = [10, game.height-2], [0, 300]
        game.paddle = 280
        self.assertIn('lost', [game.update(.05) for _ in range(3)])
        self.assertEqual(game.lives, 2)
        self.assertTrue(game.stuck)

    def test_clearing_all_bricks_starts_a_faster_level(self):
        game = BrickGame(300, 200, seed=2)
        game.bricks = {(0, 0)}
        x0, y0, x1, y1 = game.brick_rect(0, 0)
        game.stuck, game.ball, game.velocity = False, [(x0+x1)/2, y1+5], [0, -200]
        self.assertEqual(game.update(.05), 'cleared')
        self.assertEqual(game.level, 2)
        self.assertEqual(len(game.bricks), 48)
        self.assertGreater(game.speed, 160)

    def test_game_over_after_three_lives(self):
        game = BrickGame(300, 200, seed=3)
        for _ in range(3):
            game.stuck, game.ball, game.velocity = False, [5, 199], [0, 300]
            game.paddle = 280
            game.update(.05)
        self.assertTrue(game.over)


class FlappyRulesTest(unittest.TestCase):
    def test_gravity_flap_points_and_crashes(self):
        game = FlappyGame(320, 214, seed=1)
        y = game.y
        game.update(.05)
        self.assertGreater(game.y, y)  # Falls.
        game.flap()
        game.update(.02)
        self.assertLess(game.vy, 0)
        game.pipes = [[game.x - game.PIPE, game.y, False]]  # Just flown through.
        self.assertEqual(game.update(.01), 'point')
        self.assertEqual(game.score, 1)
        game.pipes = [[game.x, game.y + game.gap, False]]  # Bird is outside the gap.
        self.assertEqual(game.update(.01), 'crash')
        self.assertFalse(game.alive)
        game.reset()
        game.y, game.vy = game.height - 3, 100
        self.assertEqual(game.update(.02), 'crash')  # Hit the ground.

    def test_pipes_keep_coming_with_the_gap_on_screen(self):
        game = FlappyGame(240, 294, seed=2)
        for _ in range(400):
            ahead = [p for p in game.pipes if p[0] + game.PIPE/2 + game.RADIUS >= game.x]
            game.vy, game.y = 0, (ahead[0][1] if ahead else game.height/2)  # Fly the gaps.
            game.update(.05)
        self.assertGreater(game.score, 5)
        for x, centre, _ in game.pipes:
            self.assertTrue(game.gap/2 < centre < game.height - game.gap/2)


class ArcadeAppsTest(unittest.TestCase):
    def tearDown(self):
        app.set_rotation(0)

    def test_bricks_drag_launch_and_record(self):
        for rotation in (0, 90):
            app.set_rotation(rotation)
            saved = []
            bricks = app.BrickApp(save_best=saved.append, seed=1)
            bricks.tap(*centre(app.SnakeApp.dialog_rects()[1]), 0)
            bricks.hold((50, app.H-20), .1)
            self.assertEqual(bricks.game.paddle, 50)
            bricks.tap(app.W/2, app.H/2, .2)  # Tap the court: launch.
            self.assertFalse(bricks.game.stuck)
            bricks.game.score, bricks.game.lives = 120, 1
            bricks.game.ball, bricks.game.velocity = [5, bricks.game.height-1], [0, 300]
            bricks.game.paddle = app.W - 40
            for t in (.3, .35, .4):
                bricks.render(t, datetime.now(), ())
            self.assertEqual(bricks.state, 'over')
            self.assertEqual(saved, [120])
            self.assertEqual(bricks.render(.5, datetime.now(), ()).size, (app.W, app.H))

    def test_flappy_flaps_on_press_not_release(self):
        for rotation in (0, 90):
            app.set_rotation(rotation)
            flappy = app.FlappyApp(seed=2)
            flappy.tap(*centre(app.SnakeApp.dialog_rects()[1]), 0)
            self.assertTrue(flappy.running)
            flappy.game.vy = 50
            flappy.hold((100, 150), .1)  # Finger down: flap now.
            self.assertEqual(flappy.game.vy, flappy.game.FLAP)
            flappy.game.vy = 50
            flappy.hold((100, 150), .15)  # Still held: no second flap.
            flappy.tap(100, 150, .2)  # Release tap after a press: ignored.
            flappy.hold(None, .2)
            self.assertEqual(flappy.game.vy, 50)
            flappy.tap(100, 150, .3)  # A tap with no press seen still flaps.
            self.assertEqual(flappy.game.vy, flappy.game.FLAP)
            flappy.hold((app.W-20, 10), .4)  # Finger on PAUSE: not a flap.
            flappy.tap(app.W-20, 10, .5)
            self.assertEqual(flappy.state, 'paused')
            self.assertEqual(flappy.render(.5, datetime.now(), ()).size, (app.W, app.H))

    def test_flappy_crash_saves_best(self):
        saved = []
        flappy = app.FlappyApp(best=1, save_best=saved.append, seed=3)
        flappy.tap(*centre(app.SnakeApp.dialog_rects()[1]), 0)
        flappy.game.score = 4
        flappy.game.y, flappy.game.vy = flappy.game.height - 2, 200
        flappy.render(.05, datetime.now(), ())
        flappy.render(.1, datetime.now(), ())
        self.assertEqual(flappy.state, 'over')
        self.assertEqual(saved, [4])


class GamesLoopTest(unittest.TestCase):
    def test_menu_tetris_and_pong_through_the_real_loop(self):
        import test_panel_app as t
        # Landscape: APPS tab, GAMES tile, TETRIS, START, DROP, PAUSE, QUIT,
        # PONG, START, PAUSE (header), QUIT, BRICKS..., FLAPPY..., BACK, EXIT tab, CLOSE.
        drop = centre([r for r, a, _ in app.tetris_layout()[4] if a == 'drop'][0])
        script = [(.5, 136, 222), (1.1, 240, 70), (1.7, 237, 62), (2.3, 100, 170),
                  (2.9, *drop), (3.5, 300, 12), (4.1, 220, 170),
                  (4.7, 80, 128), (5.3, 100, 170), (5.9, 300, 12), (6.5, 220, 170),
                  (7.1, 237, 128), (7.7, 100, 170), (8.3, 160, 120), (8.9, 300, 12),
                  (9.5, 220, 170),                       # BRICKS: start, launch, pause, quit
                  (10.1, 80, 194), (10.7, 100, 170), (11.3, 300, 12),
                  (11.9, 220, 170),                      # FLAPPY: start, pause, quit
                  (12.5, 237, 194), (13.1, 300, 222), (13.7, 160, 150)]
        t.FakeTouch.timeout = 30
        runner = t.MainLoopTest('test_leaving_camera_cancels_timer')
        data, _ = runner.run_app(script)
        frames = t.FakeScreen.frames
        self.assertTrue(frames)
        self.assertEqual(frames[-1].size, (320, 240))
        scores = data / 'games.json'
        if scores.exists():  # Only written when a record is set.
            self.assertIsInstance(json.loads(scores.read_text()), dict)


if __name__ == '__main__':
    unittest.main()
