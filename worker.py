"""Worker that plays a set number of games against a bot in order to test it.

For game rules see:
http://en.wikipedia.org/wiki/Battleship_(game)
"""

import logging.config
import os
import os.path
import random
import signal
import subprocess
import time
import traceback
import yaml
from data import UsersDataSync as UsersData
from queue import BotQueue


# Grids ------------------------------------------------------------------------

class ShipGridSquareState(object):
    SEA =       0
    SHIP =      1


class ShotGridSquareState(object):
    UNKNOWN =   0
    MISS =      -1
    HIT =       1


class Grid(object):

    SIZE = 10

    def __init__(self, init_val):
        """Populate the grid with an initial value."""
        self.squares = [init_val] * (self.SIZE ** 2)

    def get(self, x, y):
        return self.squares[(y * self.SIZE) + x]

    def put(self, x, y, val):
        self.squares[(y * self.SIZE) + x] = val

    def valid_coord(self, x, y):
        """Return whether x,y are valid grid coordinates."""
        return x >= 0 and x < self.SIZE and y >= 0 and y < self.SIZE

    def rand_square(self):
        """Return the coordinates of a random grid square."""
        x = random.randint(0, self.SIZE-1)
        y = random.randint(0, self.SIZE-1)
        return x, y

    def __str__(self):
        """Return string representation of the grid.

        Used for serialising the grid for transmission to bot script.
        """
        return ','.join(map(str, self.squares))

    @classmethod
    def index_to_coord(cls, i):
        y = i / cls.SIZE
        x = i % cls.SIZE
        return x, y


# Ship Manager -----------------------------------------------------------------

class ShipManager(object):

    SHIPS = [
        5,  # aircraft carrier
        4,  # battleship
        3,  # submarine
        3,  # destroyer (or cruiser)
        2,  # patrol board (or destroyer)
        ]

    @classmethod
    def arrange_on_grid(cls, ship_grid):
        """Randomly arrange the ships `SHIPS` in the grid `ship_grid`.

        To place a ship, first a random start square in the grid is picked, 
        then a random orientation is picked (horizontal, vertical). The 
        sequence of coordinates in the grid that would occupy the ship given 
        the random start square and orientation is then identified. An attempt 
        to place the ship in this sequence of coordinates is then made. If the 
        attempt fails (see `attempt_to_place_ship_in_seq' for possible reasons) 
        then start again by picking a new random start square.

        Finish when all ships have been successfully placed.
        """

        for ship in cls.SHIPS:
            while True:
                r_x, r_y = ship_grid.rand_square()
                if random.choice([True, False]):
                    # vertical
                    seq = [(x, r_y) for x in range(r_x, r_x + ship)]
                else:
                    # horizontal
                    seq = [(r_x, y) for y in range(r_y, r_y + ship)]
                success = \
                    cls._attempt_to_place_ship_in_seq(ship_grid, ship, seq)
                if success:
                    break

    @staticmethod
    def _attempt_to_place_ship_in_seq(ship_grid, ship, seq):
        """Attempt to place the ship of length `ship` in the grid `ship_grid`
        in the squares identified by the list of coordinates `seq`.

        The attempt will fail if:
            * Any coordinates are invalid for the grid
            * A square identified by a coordinate already contains a ship part

        Returns whether the ship was successfully placed.
        """

        for x, y in seq:
            if not ship_grid.valid_coord(x, y) or \
                ship_grid.get(x, y) != ShipGridSquareState.SEA:
                return False

        for x, y in seq:
            ship_grid.put(x, y, ShipGridSquareState.SHIP)
        return True


# Game Manager -----------------------------------------------------------------

class GameManager(object):

    _log = logging.getLogger("worker")
    _BOT_MOVE_TIMEOUT = 10 # max secs a bot can take to make a move

    # TODO allow a seed to be passed
    @classmethod
    def play(cls, bot_path):
        """Generate a random ship arragement and play the game until the bot
        has hit/sunk all ships. The number of moves taken by the bot is
        returned.

        A BotIllegalMoveException is raised if the bot attempts to play an
        illegal move.

        Returns a summary of the game.

            `ships`: a list of grid squares indicating which are occupied by
                ships (1) and which are just sea (0)

            `moves`: a list of moves made by the bot and their outcome, of the
                form [(move_grid_index, hit_or_miss), ...] eg:

                    [(3, -1), (13, 1), (14, 1), (39, -1), ...]

        """

        bot_id = os.path.split(bot_path)[1]
        cls._log.info("%s bot started game" % bot_id)

        # init game state
        ship_grid = Grid(ShipGridSquareState.SEA)
        shot_grid = Grid(ShotGridSquareState.UNKNOWN)
        ShipManager.arrange_on_grid(ship_grid)

        # repeatedly ask the bot to play moves until all ships are hit/sunk
        moves = []
        while not cls._all_ships_hit(shot_grid):
            move = cls._play_next_bot_move(bot_path, ship_grid, shot_grid)
            moves.append(move)

        # return a summary of the game
        cls._log.info(
            "%s bot completed game in %s moves" % (bot_id, len(moves)))
        return {
            "moves": moves,
            "ships": ship_grid.squares,
            }

    @staticmethod
    def _all_ships_hit(shot_grid):
        """Return whether all ships have been hit/sunk.

        This is calculated by comparing the number of hits in the shot grid 
        with the number of squares occupied by ships in the ship grid.
        """
        hits = filter(
            lambda x: x == ShotGridSquareState.HIT, shot_grid.squares)
        return len(hits) == sum(ShipManager.SHIPS)

    @classmethod
    def _play_next_bot_move(cls, bot_path, ship_grid, shot_grid):
        """Run the bot script against the current `shot_grid` to obtain the
        next move, then attempt to update the `shot_grid` by playing the move.
        """

        # Get next bot move from bot script. 
        # Raise `BotMoveTimeoutException` if the bot takes too long to move:
        # http://stackoverflow.com/questions/1191374/subprocess-with-timeout
        # Raise `BotErrorException` if the bot raises an error during 
        # execution.
        def alarm_handler(signum, frame):
            raise BotMoveTimeoutException({
                "game_state":   str(shot_grid),
                })
        signal.signal(signal.SIGALRM, alarm_handler)
        signal.alarm(cls._BOT_MOVE_TIMEOUT)
        try:
            bot_move = subprocess.check_output([bot_path, str(shot_grid)])
        except (subprocess.CalledProcessError, OSError):
            traceback.print_exc() # debug
            raise BotErrorException({
                "game_state":   str(shot_grid),
                })
        finally:
            signal.alarm(0)

        # validate the bot move
        try:
            bot_move = int(bot_move)
        except ValueError:
            raise BotMoveIllegalException({
                "game_state":   str(shot_grid),
                "move":         str(bot_move),
                })
        if bot_move < 0 or bot_move >= len(shot_grid.squares):
            raise BotMoveIllegalException({
                "game_state":   str(shot_grid),
                "move":         bot_move,
                })
        x, y = Grid.index_to_coord(bot_move)
        if shot_grid.get(x, y) != ShotGridSquareState.UNKNOWN:
            raise BotMoveIllegalException({
                "game_state":   str(shot_grid),
                "move":         bot_move,
                })

        # make bot move
        hit = ship_grid.get(x, y) == ShipGridSquareState.SHIP
        val = ShotGridSquareState.HIT if hit else ShotGridSquareState.MISS
        shot_grid.put(x, y, val)

        # return move summary
        return (bot_move, val)


class BotException(Exception):
    """Abstract superclass for all bot exception."""

    def __init__(self, data):
        data["type"] = self.__class__.__name__
        self.data = data


class BotMoveIllegalException(BotException): 
    """The bot made an illegal move."""


class BotMoveTimeoutException(BotException): 
    """The bot took too long to make a move."""


class BotErrorException(BotException):
    """The bot encountered an error during execution."""


# Tournament Manager -----------------------------------------------------------

class TournamentManager(object):
    """Return the average number of moves taken by a bot to win after playing
    `num_games` games.
    """

    _log = logging.getLogger("worker")

    @classmethod
    def play(cls, bot_path, num_games):

        bot_id = os.path.split(bot_path)[1]
        cls._log.info("%s bot started tournament" % bot_id)

        scores = []
        for i in range(num_games):
            summary = GameManager.play(bot_path)
            num_moves = len(summary["moves"])
            scores.append(num_moves)
            cls._log.info(
                "%s bot completed game %s/%s of tournament" % \
                    (bot_id, i+1, num_games))
        average = sum(scores) / float(num_games)
        return average


# Main -------------------------------------------------------------------------

def main():

    # init logging
    logging_conf_path = os.path.join(
        os.path.dirname(__file__), "conf", "logging.yaml")
    logging_conf = yaml.load(open(logging_conf_path))
    logging.config.dictConfig(logging_conf)

    # load config
    conf_path = os.path.join(os.path.dirname(__file__), "conf", "system.yaml")
    with open(conf_path) as f:
        conf = yaml.load(f)

    # indefinitely process bot queue
    while True:
        user_id, bot_id = BotQueue.pop_or_wait()
        bot = os.path.join(conf["bot-path"], bot_id)

        try:
            av_num_moves_to_win = TournamentManager.play(
                bot, conf["num-games-per-tournament"])
        except BotException as e:
            UsersData.update_bot_rejected(user_id, e)
        else:
            UsersData.update_bot_success(user_id, av_num_moves_to_win)


if __name__ == "__main__":
    main()

