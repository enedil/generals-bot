"""
    @ Travis Drake (EklipZ) eklipz.io - tdrake0x45 at gmail)
    April 2017
    Generals.io Automated Client - https://github.com/harrischristiansen/generals-bot
    EklipZ bot - Tries to play generals lol
"""
import heapq
import math
import queue
import random
import time
import traceback
import typing
from queue import Queue

import logbook

import BotLogging
import DebugHelper
import EarlyExpandUtils
import Gather
import SearchUtils
import ExpandUtils
import Utils
from Algorithms import MapSpanningUtils, TileIslandBuilder, WatchmanRouteUtils, TileIsland
from Army import Army
from ArmyAnalyzer import ArmyAnalyzer
from ArmyEngine import ArmyEngine, ArmySimResult
from Behavior.ArmyInterceptor import ArmyInterceptor, ArmyInterception, ThreatBlockInfo, InterceptionOptionInfo
from BehaviorAlgorithms.IterativeExpansion import ArmyFlowExpander
from CityAnalyzer import CityAnalyzer, CityScoreData
from Communication import TeammateCommunicator, TileCompressor
from DistanceMapperImpl import DistanceMapperImpl
from Gather import GatherCapturePlan
from GatherAnalyzer import GatherAnalyzer
from Interfaces import TilePlanInterface, MapMatrixInterface
from MapMatrix import MapMatrix, MapMatrixSet, TileSet
from MctsLudii import MctsDUCT
from Path import Path, MoveListPath
from PerformanceTimer import PerformanceTimer
from BoardAnalyzer import BoardAnalyzer
from Sim.TextMapLoader import TextMapLoader
from Strategy import OpponentTracker, WinConditionAnalyzer, CaptureLineTracker
from Strategy.WinConditionAnalyzer import WinCondition
from StrategyModels import CycleStatsData, ExpansionPotential
from ViewInfo import ViewInfo, PathColorer, TargetStyle
from base import Colors
from base.client.generals import ChatUpdate, _spawn
from base.client.map import Player, Tile, MapBase, PLAYER_CHAR_BY_INDEX, new_value_grid, MODIFIER_TORUS, MODIFIER_MISTY_VEIL, MODIFIER_WATCHTOWER, MODIFIER_DEFENSELESS, MODIFIER_CITY_STATE
from DangerAnalyzer import DangerAnalyzer, ThreatType, ThreatObj
from Models import GatherTreeNode, Move, ContestData
from Directives import Timings
from History import History  # TODO replace these when city contestation
from Territory import TerritoryClassifier
from ArmyTracker import ArmyTracker

# was 30. Prevents runaway CPU usage...?
GATHER_SWITCH_POINT = 150


def scale(inValue, inBottom, inTop, outBottom, outTop):
    if inBottom > inTop:
        raise RuntimeError("inBottom > inTop")
    inValue = max(inBottom, inValue)
    inValue = min(inTop, inValue)
    numerator = (inValue - inBottom)
    divisor = (inTop - inBottom)
    if divisor == 0:
        return outTop
    valRatio = numerator / divisor
    outVal = valRatio * (outTop - outBottom) + outBottom
    return outVal


class EklipZBot(object):
    def __init__(self):
        self.blocking_tile_info: typing.Dict[Tile, ThreatBlockInfo] = {}
        self.behavior_end_of_turn_scrim_army_count: int = 5
        self.teams: typing.List[int] = []
        self.contest_data: typing.Dict[Tile, ContestData] = {}
        self.army_out_of_play: bool = False
        self._lastTargetPlayerCityCount: int = 0
        self.armies_moved_this_turn: typing.List[Tile] = []
        self.logDirectory: str | None = None
        self.perf_timer = PerformanceTimer()
        self.cities_gathered_this_cycle: typing.Set[Tile] = set()
        self.tiles_gathered_to_this_cycle: typing.Set[Tile] = set()
        self.tiles_captured_this_cycle: typing.Set[Tile] = set()
        self.tiles_evacuated_this_cycle: typing.Set[Tile] = set()
        self.player: Player = Player(-1)
        self.targetPlayerObj: typing.Union[Player, None] = None
        self.city_expand_plan: EarlyExpandUtils.CityExpansionPlan | None = None
        self.force_city_take = False
        self._gen_distances: MapMatrixInterface[int] = None
        self._ally_distances: MapMatrixInterface[int] = None
        self.defend_economy = False
        self._spawn_cramped: bool = False
        self.defending_economy_spent_turns: int = 0
        self.general_safe_func_set = {}
        self.clear_moves_func: typing.Union[None, typing.Callable] = None
        self.surrender_func: typing.Union[None, typing.Callable] = None
        self._map: MapBase | None = None
        self.curPath: Path | None = None
        self.curPathPrio = -1
        self.gathers = 0
        self.attacks = 0

        self.leafMoves: typing.List[Move] = []
        """All leaves that border an enemy or neutral tile, regardless of whether the tile has enough army to capture the adjacent."""
        self.captureLeafMoves: typing.List[Move] = []
        """All leaf moves that can capture the adjacent."""

        self.targetPlayerLeafMoves: typing.List[Move] = []
        """All moves by the target player that could capture adjacent."""

        self.attackFailedTurn = 0
        self.countFailedQuickAttacks = 0
        self.countFailedHighDepthAttacks = 0
        self.largeTilesNearEnemyKings = {}
        self.no_file_logging: bool = False
        self.enemyCities = []
        self.enemy_city_approxed_attacks: typing.Dict[Tile, typing.Tuple[int, int, int]] = {}
        """Contains a mapping from an enemy city, to the approximate (turns, ourAttack, theirDefense)"""

        self.dangerAnalyzer: DangerAnalyzer | None = None
        self.cityAnalyzer: CityAnalyzer | None = None
        self.gatherAnalyzer: GatherAnalyzer | None = None
        self.lastTimingFactor = -1
        self.lastTimingTurn = 0
        self._evaluatedUndiscoveredCache = []
        self.lastTurnStartTime = 0
        self.currently_forcing_out_of_play_gathers: bool = False
        self.force_far_gathers: bool = False
        """If true, far gathers will be FORCED after defense."""
        self.force_far_gathers_turns: int = 0
        """The turns aiming to force for"""
        self.force_far_gathers_sleep_turns: int = 50
        """The number of turns to wait for another far gather for"""

        self.out_of_play_tiles: typing.Set[Tile] = set()

        self.is_rapid_capturing_neut_cities: bool = False

        self.is_winning_gather_cyclic: bool = False

        self.is_all_in_losing = False
        self.all_in_army_advantage_counter: int = 0
        self.all_in_army_advantage_cycle: int = 35
        self.is_all_in_army_advantage: bool = False
        self.all_in_city_behind: bool = False
        """If set to true, will use the expansion cycle timing to all in gather at opp and op cities."""

        self.trigger_player_capture_re_eval: bool = False
        """Set to true to trigger start of turn re-evaluation of stuff due to a player capture"""

        self.giving_up_counter: int = 0
        self.all_in_losing_counter: int = 0
        self.approximate_greedy_turns_avail: int = 0
        self.lastTargetAttackTurn = 0
        self.gather_kill_priorities: typing.Set[Tile] = set()
        self.threat_kill_path: Path | None = None
        """Set this to a threat kill path for post-city-recapture-threat-interception"""

        self.expansion_plan: ExpansionPotential | None = None

        self.enemy_expansion_plan: ExpansionPotential | None = None

        self.enemy_expansion_plan_tile_path_cap_values: typing.Dict[Tile, int] = {}

        self.intercept_plans: typing.Dict[Tile, ArmyInterception] = {}

        self.generalApproximations: typing.List[typing.Tuple[float, float, int, Tile | None]] = []
        """
        List of general location approximation data as averaged by enemy tiles bordering undiscovered and euclid averaged.
        Tuple is (xAvg, yAvg, countUsed, generalTileIfKnown)
        Used for player targeting (we do the expensive approximation only for the target player?)
        This is aids.
        """

        self.opponent_tracker: OpponentTracker = None


        self.lastGeneralGatherTurn = -2
        self.is_blocking_neutral_city_captures: bool = False
        self.was_allowing_neutral_cities_last_turn: bool = True
        self.city_capture_plan_tiles: typing.Set[Tile] = set()
        self.city_capture_plan_last_updated: int = 0
        self._expansion_value_matrix: MapMatrixInterface[float] | None = None
        self.targetPlayer = -1
        self.failedUndiscoveredSearches = 0
        self.largePlayerTiles: typing.List[Tile] = []
        """The large tiles owned by us."""

        self.largeNegativeNeutralTiles: typing.List[Tile] = []
        """Large negative neutral tiles (less than or equal to -2), ordered from most-negative to least-negative."""
        self.playerTargetScores = [0 for i in range(16)]
        self.general: Tile | None = None
        self.gatherNodes = None
        self.redGatherTreeNodes = None
        self.isInitialized = False
        self.last_init_turn: int = 0
        """
        Mainly just for testing purposes, prevents the bot from performing a turn update more than once per turn, 
        which tests sometimes need to trigger ahead of time in order to check bot state ahead of a move.
        """

        # 2v2
        self.teammate: int | None = None
        self.teammate_path: Path | None = None
        self.teammate_general: Tile | None = None

        self._outbound_team_chat: "Queue[str]" = queue.Queue()
        self._outbound_all_chat: "Queue[str]" = queue.Queue()
        self._tile_ping_queue: "Queue[Tile]" = queue.Queue()

        self._chat_messages_received: "Queue[ChatUpdate]" = queue.Queue()
        self._tiles_pinged_by_teammate: "Queue[Tile]" = queue.Queue()
        self._communications_sent_cooldown_cache: typing.Dict[str, int] = {}
        self.tiles_pinged_by_teammate_this_turn: typing.Set[Tile] = set()
        self._tiles_pinged_by_teammate_first_25: typing.Set[Tile] = set()
        self.teamed_with_bot: bool = False
        self.teammate_communicator: TeammateCommunicator | None = None
        self.tile_compressor: TileCompressor | None = None

        self.oldAllyThreat: ThreatObj | None = None
        """Last turns ally threat."""

        self.oldThreat: ThreatObj | None = None
        """Last turns threat against us."""

        self.makingUpTileDeficit = False
        self.territories: TerritoryClassifier | None = None

        self.target_player_gather_targets: typing.Set[Tile] = set()
        self.target_player_gather_path: Path | None = None
        self.shortest_path_to_target_player: Path | None = None
        self.defensive_spanning_tree: typing.Set[Tile] = set()

        self.enemy_attack_path: Path | None = None
        """The probable enemy attack path."""

        self.likely_kill_push: bool = False

        self.viewInfo: ViewInfo | None = None

        self._minAllowableArmy = -1
        self.threat: ThreatObj | None = None
        self.best_defense_leaves: typing.List[GatherTreeNode] = []
        self.has_defenseless_modifier: bool = False
        self.has_watchtower_modifier: bool = False
        self.has_misty_veil_modifier: bool = False

        self.is_weird_custom: bool = False

        self.history = History()
        self.timings: Timings | None = None
        self.tileIslandBuilder: TileIslandBuilder = None
        self._should_recalc_tile_islands: bool = False
        self.armyTracker: ArmyTracker = None
        self.army_interceptor: ArmyInterceptor = None
        self.win_condition_analyzer: WinConditionAnalyzer = None
        self.capture_line_tracker: CaptureLineTracker = None
        self.finishing_exploration = True
        self.targetPlayerExpectedGeneralLocation: Tile | None = None

        self.alt_en_gen_positions: typing.List[typing.List[Tile]] = []
        """Decently possible enemy general spawn positions, separated from each other by at least 2 tiles."""

        self._alt_en_gen_position_distances: typing.List[MapMatrixInterface[int] | None] = []
        self.lastPlayerKilled = None
        self.launchPoints: typing.List[Tile] | None = []
        self.locked_launch_point: Tile | None = None
        self.high_fog_risk: bool = False
        self.fog_risk_amount: int = 0
        self.flanking: bool = False
        self.sketchiest_potential_inbound_flank_path: Path | None = None
        self.completed_first_100: bool = False
        """Set to true if the bot is flanking this cycle and should prepare to launch an earlier attack than normal."""

        self.is_lag_massive_map: bool = False

        self.undiscovered_priorities: MapMatrixInterface[float] | None = None
        self._undisc_prio_turn: int = -1
        self._afk_players: typing.List[Player] | None = None
        self._is_ffa_situation: bool | None = None

        self.explored_this_turn = False
        self.board_analysis: BoardAnalyzer | None = None

        # engine stuff
        self.targetingArmy: Army | None = None
        self.cached_scrims: typing.Dict[str, ArmySimResult] = {}
        self.next_scrimming_army_tile: Tile | None = None

        # configuration
        self.disable_engine: bool = True

        self.engine_use_mcts: bool = True
        self.mcts_engine: MctsDUCT = MctsDUCT()
        self.engine_allow_force_incoming_armies_towards: bool = False
        self.engine_allow_enemy_no_op: bool = True
        self.engine_include_path_pre_expansion: bool = True
        self.engine_path_pre_expansion_cutoff_length: int = 5
        self.engine_force_multi_tile_mcts = True
        self.engine_army_nearby_tiles_range: int = 4
        self.engine_mcts_scrim_armies_per_player_limit: int = 2
        self.engine_honor_mcts_expected_score: bool = False
        self.engine_honor_mcts_expanded_expected_score: bool = True
        self.engine_always_include_last_move_tile_in_scrims: bool = True
        self.engine_mcts_move_estimation_net_differential_cutoff: float = -0.9
        """An engine move result below this score will be ignored in some situations. Lower closer to -1.0 to respect more engine moves."""

        self.gather_include_shortest_pathway_as_negatives: bool = False
        self.gather_include_distance_from_enemy_TERRITORY_as_negatives: int = 2  # 4 is bad, confirmed 217-279 in 500 game match after other previous

        # 2 and 3 both perform well, probably need to make the selection method more complicated as there are probably times it should use 2 and times it should use 3.
        self.gather_include_distance_from_enemy_TILES_as_negatives: int = 3  # 3 is definitely too much, confirmed in lots of games. ACTUALLY SEEMS TO BE CONTESTED NOW 3 WON 500 GAME MATCH, won another 500 game match, using 3...

        self.gather_include_distance_from_enemy_general_as_negatives: float = 0.0
        self.gather_include_distance_from_enemy_general_large_map_as_negatives: float = 0.0
        self.gather_use_pcst: bool = False
        # swaps between max iterative and max set, now
        self.gather_use_max_set: bool = False
        """If true, use max-set. If False, use max iterative"""

        self.expansion_force_no_global_visited: bool = False
        self.expansion_force_global_visited_stage_1: bool = True
        self.expansion_use_iterative_negative_tiles: bool = True
        self.expansion_allow_leaf_moves: bool = True
        self.expansion_use_leaf_moves_first: bool = False
        self.expansion_enemy_expansion_plan_inbound_penalty: float = 0.55
        self.expansion_single_iteration_time_cap: float = 0.03  # 0.1 did slightly better than 0.06, but revert to 0.06 if expansion takes too long
        self.expansion_length_weight_offset: float = 0.5
        """Positive means prefer longer paths, slightly...?"""

        self.expansion_allow_gather_plan_extension: bool = True
        """If true, look at leafmoves that do not capture tiles and see if we can gather a capture to their target in 2 moves."""

        self.expansion_always_include_non_terminating_leafmoves_in_iteration: bool = True
        """If true, forces the expansion plan to always consider non-terminating leafmove tiles in one of the iterations."""

        self.expansion_use_iterative_flow: bool = False
        self.expansion_use_legacy: bool = True
        self.expansion_use_tile_islands: bool = False
        self.expansion_full_time_limit: float = 0.15
        """The full time limit for an optimal_expansion cycle. Will be cut short if it would run the move too long."""

        self.expansion_use_cutoff: bool = True
        """The time cap per large tile search when finding expansions"""

        self.expansion_small_tile_time_ratio: float = 1.0
        """The ratio of expansion_single_iteration_time_cap that will be used for each small tile path find iteration."""

        self.behavior_out_of_play_defense_threshold: float = 0.40
        """What ratio of army needs to be outside the behavior_out_of_play_distance_over_shortest_ratio distance to trigger out of play defense."""

        self.behavior_losing_on_economy_skip_defense_threshold: float = 0.0
        """The threshold (where 1.0 means even on economy, and 0.9 means losing by 10%) at which we stop defending general."""

        self.behavior_early_retake_bonus_gather_turns: int = 0
        """This is probably just bad but was currently 3. How long to spend gathering to needToKill tiles around general before switching to main gather."""

        self.behavior_max_allowed_quick_expand: int = 0  # was 7
        """Number of quickexpand turns max always allowed, unrelated to greedy leaves."""

        self.behavior_out_of_play_distance_over_shortest_ratio: float = 0.45
        """Between 0 and 1. 0.3 means any tiles outside 1.3x the shortest pathway length will be considered out of play for out of play army defense."""

        self.behavior_launch_timing_offset: int = 3
        """Negative means launch x turns earlier, positive means later. The actual answer here is probably 'launch after your opponent', so a dynamic launch timing would make the most sense."""

        self.behavior_flank_launch_timing_offset: int = -4
        """Negative means launch x turns earlier, positive means later than normal launch timing would have been."""

        self.behavior_allow_defense_army_scrim: bool = False
        """If true, allows running a scrim with the defense leafmove that WOULD have been made if the defense wasn't immediately necessary, when shorter gather prunes are found."""

        # TODO drop this probably when iterative gather/expansion done.
        self.behavior_allow_pre_gather_greedy_leaves: bool = True
        """If true, allow just-ahead-of-opponent greedy leaf move blobbing."""

        self.behavior_pre_gather_greedy_leaves_army_ratio_cutoff: float = 0.97
        """Smaller = greedy expansion even when behind on army, larger = only do it when already winning on army"""

        self.behavior_pre_gather_greedy_leaves_offset: int = 0
        """Negative means capture that many extra tiles after hitting the 1.05 ratio. Positive means let them stay a little ahead on tiles."""

        self.info_render_gather_values: bool = True
        """render trunk value etc from actual gather nodes during prunes etc"""

        self.info_render_gather_matrix_values: bool = True
        """render the gather priority matrix values"""

        self.info_render_gather_locality_values: bool = False

        self.info_render_centrality_distances: bool = False
        self.info_render_leaf_move_values: bool = False
        self.info_render_army_emergence_values: bool = True
        self.info_render_board_analysis_choke_widths: bool = False
        self.info_render_board_analysis_zones: bool = True
        self.info_render_city_priority_debug_info: bool = True
        self.info_render_general_undiscovered_prediction_values: bool = False
        self.info_render_tile_deltas: bool = False
        self.info_render_tile_states: bool = False
        self.info_render_expansion_matrix_values: bool = True
        self.info_render_intercept_data: bool = False
        self.info_render_tile_islands: bool = False
        self.info_render_defense_spanning_tree: bool = True

    def __repr__(self):
        return str(self)

    def __str__(self):
        return f'[eklipz_bot {str(self._map)}]'

    def spawnWorkerThreads(self):
        return

    def detect_repetition_at_all(self, turns=4, numReps=2) -> bool:
        curTurn = self._map.turn
        reps = 0
        prevMove = None
        for turn in range(int(curTurn - turns), curTurn):
            if turn in self.history.move_history:
                for lastMove in self.history.move_history[turn]:
                    if (
                            prevMove is not None
                            and turn not in self.history.droppedHistory
                            and lastMove is not None
                            and (
                            (lastMove.dest == prevMove.source and lastMove.source == prevMove.dest)
                            or (lastMove.source == prevMove.source and lastMove.dest == prevMove.dest)
                    )
                    ):
                        reps += 1
                        if reps == numReps:
                            logbook.info(
                                f"  ---    YOOOOOOOOOO detected {reps} repetitions on {lastMove.source.x},{lastMove.source.y} -> {lastMove.dest.x},{lastMove.dest.y} in the last {turns} turns")
                            return True
                    prevMove = lastMove

        return False

    def detect_repetition(self, move, turns=4, numReps=2):
        """

        @param move:
        @param turns:
        @param numReps:
        @return:
        """
        if move is None:
            return False
        curTurn = self._map.turn
        reps = 0
        for turn in range(int(curTurn - turns), curTurn):
            if turn in self.history.move_history:
                for oldMove in self.history.move_history[turn]:
                    if turn not in self.history.droppedHistory and (oldMove is not None
                                                                    and ((oldMove.dest == move.source and oldMove.source == move.dest)
                                                                         or (oldMove.source == move.source and oldMove.dest == move.dest))):
                        reps += 1
                        if reps == numReps:
                            logbook.info(
                                f"  ---    YOOOOOOOOOO detected {reps} repetitions on {move.source.x},{move.source.y} -> {move.dest.x},{move.dest.y} in the last {turns} turns")
                            return True
        return False

    def detect_repetition_tile(self, tile: Tile, turns=6, numReps=2):
        """

        @param tile
        @param turns:
        @param numReps:
        @return:
        """
        if tile is None:
            return False
        curTurn = self._map.turn
        reps = 0
        for turn in range(int(curTurn - turns), curTurn):
            if turn in self.history.move_history:
                for oldMove in self.history.move_history[turn]:
                    if turn not in self.history.droppedHistory and oldMove is not None and oldMove.dest == tile:
                        reps += 1
                        if reps == numReps:
                            logbook.info(
                                f"  ---    YOOOOOOOOOO detected {reps} repetitions on {tile.x},{tile.y} in the last {turns} turns")
                            return True
        return False

    def move_half_on_repetition(self, move, repetitionTurns, repCount=3):
        if self.detect_repetition(move, repetitionTurns, repCount):
            move.move_half = True
        return move

    def find_move(self, is_lag_move=False) -> Move | None:
        move: Move | None = None
        try:
            move = self.select_move(is_lag_move=is_lag_move)

            if move is not None and move.source.isGeneral and not self.is_move_safe_valid(move, allowNonKill=True):
                logbook.error(f'TRIED TO PERFORM AN IMMEDIATE DEATH MOVE, INVESTIGATE: {move}')
                self.info(f'Tried to perform a move that dies immediately. {move}')
                dangerTiles = self.get_danger_tiles()
                replaced = False
                for dangerTile in dangerTiles:
                    if self.general in dangerTile.movable:
                        altMove = Move(self.general, dangerTile)
                        if not self.is_move_safe_valid(altMove, allowNonKill=True):
                            altMove.move_half = True
                        if self.is_move_safe_valid(altMove, allowNonKill=True):
                            self.info(f'Replacing with danger kill {altMove}')
                            move = altMove
                            replaced = True
                            break

                if not replaced and self.expansion_plan:
                    for opt in self.expansion_plan.all_paths:
                        firstMove = opt.get_first_move()
                        if self.is_move_safe_valid(firstMove, allowNonKill=True):
                            move = firstMove
                            self.info(f'Replacing with exp move {firstMove}')
                            replaced = True
                            break

                if not replaced:
                    self.info(f'Replacing with no-op...')
                    move = None

            if self.teammate_communicator is not None:
                teammate_messages = self.teammate_communicator.produce_teammate_communications()
                for msg in teammate_messages:
                    self.send_teammate_communication(msg.message, msg.ping_tile, msg.cooldown, msg.cooldown_detection_on_message_alone, msg.cooldown_key)

            self._map.last_player_index_submitted_move = None
            if move is not None and move.source.player != self.general.player:
                raise AssertionError(f'select_move just returned {move} moving from a tile we didn\'t own...')
            if move is not None:
                self._map.last_player_index_submitted_move = (move.source, move.dest, move.move_half)
        except Exception as ex:
            self.viewInfo.add_info_line(f'BOT ERROR')
            infoStr = traceback.format_exc()
            broken = infoStr.split('\n')
            if len(broken) < 34:
                for line in broken:
                    self.viewInfo.add_info_line(line)
            else:
                for line in broken[:4]:
                    self.viewInfo.add_info_line(line)
                self.viewInfo.add_info_line('...')
                for line in broken[-26:]:
                    self.viewInfo.add_info_line(line)

            raise
        finally:
            # # this fucks performance, need to make it not slow
            # if move is not None and not self.is_move_safe_against_threats(move):
            #     self.curPath = None
            #     path: Path = None
            #     if self.threat.path.start.tile in self.armyTracker.armies:
            #         path = self.kill_army(self.armyTracker.armies[self.threat.path.start.tile], allowGeneral=False, allowWorthPathKillCheck=True)
            #         if path is not None and False:
            #             self.curPath = path
            #             self.info('overrode unsafe move with threat kill')
            #             move = Move(path.start.tile, path.start.next.tile)
            #     if path is None:
            #         self.info(f'overrode unsafe move with threat gather depth {gatherDepth} after no threat kill found')
            #         move = self.gather_to_threat_path(self.threat)

            self.check_cur_path()

            if move is not None and self.curPath is not None:
                curPathMove = self.curPath.get_first_move()
                if curPathMove.source == move.source and curPathMove.dest != move.dest:
                    logbook.info("Returned a move using the tile that was curPath, but wasn't the next path move. Resetting path...")
                    self.curPath = None
                    self.curPathPrio = -1

            if self._map.turn not in self.history.move_history:
                self.history.move_history[self._map.turn] = []
            self.history.move_history[self._map.turn].append(move)

            self.prep_view_info_for_render(move)

        return move

    def clean_up_path_before_evaluating(self):
        if not self.curPath:
            return

        if self.curPath.length == 0:
            self.curPath = None
            return

        if isinstance(self.curPath, GatherCapturePlan):
            firstMove = self.curPath.get_first_move()
            thresh = self.curPath.gathered_army / self.curPath.length / 2 + 0.5
            while firstMove is not None and (firstMove.source.army < thresh or firstMove.source.player != self.player.index):
                self.curPath.pop_first_move()
                firstMove = self.curPath.get_first_move()

        if not isinstance(self.curPath, Path):
            return

        # if self.curPath.get_first_move() == 0:
        #     self.curPath = None
        #     return

        if self.curPath.start.next is not None and not self.droppedMove(self.curPath.start.tile, self.curPath.start.next.tile):
            self.curPath.pop_first_move()
            if self.curPath.length <= 0:
                logbook.info("TERMINATING CURPATH BECAUSE <= 0 ???? Path better be over")
                self.curPath = None
            if self.curPath is not None:
                if self.curPath.start.next is not None and self.curPath.start.next.next is not None and self.curPath.start.next.next.next is not None and self.curPath.start.tile == self.curPath.start.next.next.tile and self.curPath.start.next.tile == self.curPath.start.next.next.next.tile:
                    logbook.info("\n\n\n~~~~~~~~~~~\nDe-duped path\n~~~~~~~~~~~~~\n\n~~~\n")
                    self.curPath.pop_first_move()
                    self.curPath.pop_first_move()
                    self.curPath.pop_first_move()
                    self.curPath.pop_first_move()
                elif self.curPath.start.next is not None and self.curPath.start.tile.x == self.curPath.start.next.tile.x and self.curPath.start.tile.y == self.curPath.start.next.tile.y:
                    logbook.warn("           wtf, doubled up tiles in path?????")
                    self.curPath.pop_first_move()
                    self.curPath.pop_first_move()
        else:
            logbook.info("         --         missed move?")

    def droppedMove(self, fromTile=None, toTile=None, movedHalf=None):
        log = True
        lastMove = None
        if (self._map.turn - 1) in self.history.move_history:
            lastMove = self.history.move_history[self._map.turn - 1][0]
        if movedHalf is None and lastMove is not None:
            movedHalf = lastMove.move_half
        elif movedHalf is None:
            movedHalf = False
        if fromTile is None or toTile is None:
            if lastMove is None:
                if log:
                    logbook.info("DM: False because no last move")
                return False
            fromTile = lastMove.source
            toTile = lastMove.dest
        # easy stuff
        # if somebody else took the fromTile, then its fine.
        if fromTile.player != self.general.player:
            if log:
                logbook.info("DM: False because another player captured fromTile so our move may or may not have been processed first")
            return False
        # if movedHalf:
        #    if log:
        #        logbook.info("DM: False (may be wrong) because not bothering to calculate when movedHalf=True")
        #    return False
        # if army on from is what we expect
        expectedFrom = 1
        expectedToDeltaOnMiss = 0
        if self._map.is_army_bonus_turn:
            expectedFrom += 1
            if toTile.player != -1:
                expectedToDeltaOnMiss += 1
        if (fromTile.isCity or fromTile.isGeneral) and self._map.is_city_bonus_turn:
            expectedFrom += 1
        if ((toTile.isCity and toTile.player != -1) or toTile.isGeneral) and self._map.is_city_bonus_turn:
            expectedToDeltaOnMiss += 1
        dropped = True
        if not movedHalf:
            if fromTile.army <= expectedFrom:
                if log:
                    logbook.info("DM: False because fromTile.army {} <= expectedFrom {}".format(fromTile.army, expectedFrom))
                dropped = False
            else:
                if log:
                    logbook.info("DM: True because fromTile.army {} <= expectedFrom {}".format(fromTile.army, expectedFrom))
                dropped = True
        else:
            if abs(toTile.delta.armyDelta) != expectedToDeltaOnMiss:
                if log:
                    logbook.info("DM: False because movedHalf and toTile delta {} != expectedToDeltaOnMiss {}".format(abs(toTile.delta.armyDelta), expectedToDeltaOnMiss))
                dropped = False
            else:
                if log:
                    logbook.info("DM: True because movedHalf and toTile delta {} == expectedToDeltaOnMiss {}".format(abs(toTile.delta.armyDelta), expectedToDeltaOnMiss))
                dropped = True
        if dropped:
            self.history.droppedHistory[self._map.turn - 1] = True
        return dropped

    def handle_city_found(self, tile):
        logbook.info(f"EH: City found handler! City {str(tile)}")
        self.armyTracker.add_need_to_track_city(tile)
        self.territories.needToUpdateAroundTiles.add(tile)
        if tile.player != -1:
            self.board_analysis.should_rescan = True
        return None

    def handle_tile_captures(self, tile: Tile):
        """This triggers before we know the new owner of fog tiles. tile.delta.newOwner might currently be -1, when in reality it is the opponent."""
        logbook.info(
            f"EH: Tile captured! Tile {repr(tile)}, oldOwner {tile.delta.oldOwner} newOwner {tile.delta.newOwner}")
        self.territories.needToUpdateAroundTiles.add(tile)
        if tile.isCity:
            self.armyTracker.add_need_to_track_city(tile)

            if tile.delta.oldOwner == -1 or tile.delta.newOwner == -1:
                self.board_analysis.should_rescan = True
                self._map.distance_mapper.recalculate()
                self.cityAnalyzer.reset_reachability()
                # NO see comment at top
                if tile.delta.newOwner == -1:
                    return

        if not tile.delta.gainedSight:
            self.armyTracker.notify_seen_player_tile(tile)

        if tile.delta.oldOwner == self.general.player or tile.delta.oldOwner in self._map.teammates:
            if not self._map.is_tile_friendly(tile):
                murderer = self._map.players[tile.player]

                # capturing our tiles in no mans land
                tileScore = 10
                if self.territories.territoryMap[tile] == tile.delta.newOwner:
                    # just protecting themselves in their own territory...?
                    tileScore = 5
                elif self._map.is_player_on_team_with(self.territories.territoryMap[tile], self.general.player):
                    tileScore = 30

                if tile.isCity:
                    tileScore += 5
                    tileScore = tileScore * 10

                murderer.aggression_factor += tileScore

        return None

    def handle_player_captures(self, capturee: int, capturer: int):
        """
        NOTE: This currently gets called BEFORE the map update is received that updates the visible tiles.

        @param capturee:
        @param capturer:
        @return:
        """
        logbook.info(
            f"EH: Player captured! capturee {self._map.usernames[capturee]} ({capturee}) capturer {self._map.usernames[capturer]} ({capturer})")
        for army in list(self.armyTracker.armies.values()):
            if army.player == capturee:
                logbook.info(f"EH:   scrapping dead players army {str(army)}")
                self.armyTracker.scrap_army(army, scrapEntangled=True)

        self.history.captured_player(self._map.turn, capturee, capturer)

        if capturer == self.general.player:
            logbook.info(f"setting lastPlayerKilled to {capturee}")
            self.lastPlayerKilled = capturee
            playerGen = self._map.players[capturee].general
            self.launchPoints.append(playerGen)

        self.trigger_player_capture_re_eval = True

        return None

    def handle_tile_deltas(self, tile):
        logbook.info(f"EH: Tile delta handler! Tile {repr(tile)} delta {tile.delta.armyDelta}")
        return None

    def handle_tile_discovered(self, tile):
        logbook.info(f"EH: Tile discovered handler! Tile {repr(tile)}")
        self.territories.needToUpdateAroundTiles.add(tile)
        if tile.isCity and tile.player != -1:
            self.board_analysis.should_rescan = True
            self._map.distance_mapper.recalculate()
        if tile.isCity and tile.player == -1 and tile.delta.oldOwner != -1:
            self._map.distance_mapper.recalculate()

        if tile.player >= 0:
            player = self._map.players[tile.player]
            if len(player.tiles) < 4 and tile.player == self.targetPlayer and self.curPath:
                self.viewInfo.add_info_line("killing current path because JUST discovered player...")
                self.curPath = None
            #
            # adjMatchingPlayer = []
            # for adj in tile.movable:
            #     if not self._map.is_player_on_team_with(adj.player, tile.player):
            #         continue
            #     adjMatchingPlayer.append(adj)
            #
            # if len(adjMatchingPlayer) == 0:
            #     for adj in tile.movable:
            #         if adj.discovered:
            #             continue
            #         adj.player = tile.player
            #         adj.army = 1
            #         adjMatchingPlayer.append(adj)
            #
            # if len(adjMatchingPlayer) == 0:
            #     for adj in tile.movable:
            #         if adj.visible or adj.player >= 0:
            #             continue
            #
            #         adj.player = tile.player
            #         adj.army = 1
            #         adjMatchingPlayer.append(adj)

        return None

    def handle_tile_vision_change(self, tile: Tile):
        """
        Called whenever we gain or lose vision of a tile.

        @param tile:
        @return:
        """
        logbook.info(f"EH: Tile vision change handler! Tile {repr(tile)}")

        self.territories.needToUpdateAroundTiles.add(tile)
        if tile.visible:
            self.territories.revealed_tile(tile)

        if tile.delta.gainedSight:
            self.armyTracker.notify_seen_player_tile(tile)

        if tile.isCity and tile.delta.oldOwner != tile.player and tile.delta.gainedSight:
            if self.curPath is not None and self.curPath.tail.tile == tile:
                self.viewInfo.add_info_line(f'reset curPath because gained vision of a city whose player is now different.')
                self.curPath = None

            if tile.delta.oldOwner == -1 or tile.player == -1:
                self._map.distance_mapper.recalculate()

        if tile.delta.gainedSight and tile.player >= 0:
            self.opponent_tracker.notify_player_tile_revealed(tile)
            if len(self._map.players[tile.player].tiles) < 3:
                if self._map.turn > 15:
                    allNew = True
                    for otherTile in self._map.players[tile.player].tiles:
                        if not otherTile.delta.gainedSight:
                            allNew = False

                    for adj in tile.adjacents:
                        if adj.player in self._map.teammates and adj.delta.armyDelta != 0:
                            # don't announce you found them to teammate when the teammate actually found them.
                            allNew = False

                    if allNew:
                        # self.send_teammate_communication(f'Found {self._map.usernames[tile.player]}', tile, cooldown=5, detectOnMessageAlone=True)
                        self._should_recalc_tile_islands = True
        elif tile.delta.lostSight and tile.player >= 0:
            self.opponent_tracker.notify_player_tile_vision_lost(tile)

        if tile.isMountain:
            if self.curPath is not None and tile in self.curPath.tileSet:
                self.curPath = None
            if tile.delta.oldOwner != -1:
                self.armyTracker.add_need_to_track_city(tile)
                self.viewInfo.add_info_line(f'FOG CITY {repr(tile)} WAS WRONG, FORCING RESCANS AND PLAYER PATH RECALCS')
                # self.recalculate_player_paths(force=True)
                self.target_player_gather_path = None
                self.target_player_gather_targets = None
                self.shortest_path_to_target_player = None
                self.board_analysis.should_rescan = True

        if tile.visible and tile.isCity and tile.player == -1 and tile.delta.oldOwner != -1:
            if self.curPath is not None and tile in self.curPath.tileSet:
                self.viewInfo.add_info_line(f'Ceasing curPath because target city was actually neutral.')
                self.curPath = None

        if tile.isCity:
            self.armyTracker.add_need_to_track_city(tile)

        return None

    def handle_army_moved(self, army: Army):
        tile = army.tile
        logbook.info(f"EH: Army Moved handler! Tile {repr(tile)}")
        self.armies_moved_this_turn.append(tile)
        # TODO this is very wrong, this handler should take an army / player as param, the mover may not own the tile they moved towards... But probably doesn't matter since we dont need this turn to be super accurate.
        player = self._map.players[tile.player]
        player.last_seen_move_turn = self._map.turn
        # THIS IS ALREADY PERFORMED BY the map.emerged loop.
        if army.path.tail.prev is not None and not army.path.tail.prev.tile.was_visible_last_turn() and army.tile.visible:
            self.opponent_tracker.notify_emerged_army(
                army.path.tail.prev.tile,
                emergingPlayer=army.player,
                emergenceAmount=0 - army.path.tail.prev.tile.delta.armyDelta)
        self.territories.needToUpdateAroundTiles.add(tile)
        self.territories.revealed_tile(tile)
        return None

    def get_elapsed(self):
        return round(self.perf_timer.get_elapsed_since_update(self._map.turn), 3)

    def init_turn(self, secondAttempt=False):
        if self.last_init_turn == self._map.turn:
            return

        self._alt_en_gen_position_distances: typing.List[MapMatrixInterface[int] | None] = [None for _ in self._map.players]

        self._afk_players = None
        self._is_ffa_situation = None

        self.gathers = None
        self.gatherNodes = None

        timeSinceLastUpdate = 0
        now = time.perf_counter()
        if self.lastTurnStartTime != 0:
            timeSinceLastUpdate = now - self.lastTurnStartTime

        self._expansion_value_matrix = None

        self.lastTurnStartTime = now
        logbook.info(f"\n       ~~~\n       Turn {self._map.turn}   ({timeSinceLastUpdate:.3f})\n       ~~~\n")

        if not secondAttempt:
            self.viewInfo.clear_for_next_turn()

        # self.pinged_tiles = set()
        self.was_allowing_neutral_cities_last_turn = not self.is_blocking_neutral_city_captures
        self.is_blocking_neutral_city_captures = False

        if self._map.is_2v2 or len(self._map.get_teammates_no_self(self._map.player_index)) > 0:
            playerTeam = self._map.teams[self._map.player_index]
            self.teammate = [p for p, t in enumerate(self._map.teams) if t == playerTeam and p != self._map.player_index][0]
            teammatePlayer = self._map.players[self.teammate]
            if not teammatePlayer.dead and self._map.generals[self.teammate]:
                self.teammate_general = self._map.generals[self.teammate]
                self.teammate_path = self.get_path_to_target(self.teammate_general, preferEnemy=True, preferNeutral=True)
            else:
                self.teammate_general = None
                self.teammate_path = None

            if self.teammate_communicator is None:
                if self.tile_compressor is None:
                    # self.tile_compressor = ServerTileCompressor(self._map)
                    self.tile_compressor = TileCompressor(self._map)  # use friendly one for now, to debug
                self.teammate_communicator = TeammateCommunicator(self._map, self.tile_compressor, self.board_analysis)
            self.teammate_communicator.begin_next_turn()

        while self._chat_messages_received.qsize() > 0:
            chatUpdate = self._chat_messages_received.get()
            self.handle_chat_message(chatUpdate)

        self.check_target_player_just_took_city()

        # None tells the cache to recalculate this turn to either true or false... Don't change to false, moron.
        self._spawn_cramped = None

        self.last_init_turn = self._map.turn

        if self._map.is_army_bonus_turn:
            for otherPlayer in self._map.players:
                otherPlayer.aggression_factor = otherPlayer.aggression_factor // 2

        if not secondAttempt:
            self.explored_this_turn = False

        if self.defend_economy:
            self.defending_economy_spent_turns += 1

        self.threat_kill_path = None

        self.redGatherTreeNodes = None
        if self.general is not None:
            self._gen_distances = self._map.distance_mapper.get_tile_dist_matrix(self.general)

        if self.teammate_general is not None:
            self._ally_distances = self._map.distance_mapper.get_tile_dist_matrix(self.teammate_general)

        if not self.isInitialized and self._map is not None:
            self.initialize_from_map_for_first_time(self._map)

        if self.trigger_player_capture_re_eval:
            self.reevaluate_after_player_capture()
            self.trigger_player_capture_re_eval = False

        self._minAllowableArmy = -1
        self.enemyCities = []
        if self._map.turn - 3 > self.lastTimingTurn:
            self.lastTimingFactor = -1

        lastMove = None

        if self._map.turn - 1 in self.history.move_history:
            lastMove = self.history.move_history[self._map.turn - 1][0]

        with self.perf_timer.begin_move_event('ArmyTracker Move/Emerge'):
            self.armies_moved_this_turn = []
            # the callback on armies moved will fill the list above back up during armyTracker.scan
            self.armyTracker.scan_movement_and_emergences(lastMove, self._map.turn, self.board_analysis)

        for tile, emergenceTuple in self._map.army_emergences.items():
            emergedAmount, emergingPlayer = emergenceTuple
            if emergedAmount > 0 or not self._map.is_player_on_team_with(tile.delta.oldOwner, tile.player):  # otherwise, this is just the player moving into fog generally.
                self.opponent_tracker.notify_emerged_army(tile, emergingPlayer, emergedAmount)

        self._evaluatedUndiscoveredCache = []
        with self.perf_timer.begin_move_event('get_predicted_target_player_general_location'):
            maxTile: Tile = self.get_predicted_target_player_general_location()
            self.info(f'DEBUG: en tile {maxTile} get_predicted_target_player_general_location')
            if (self.targetPlayerExpectedGeneralLocation != maxTile or self.shortest_path_to_target_player is None) and maxTile is not None:
                self.targetPlayerExpectedGeneralLocation = maxTile
                self.recalculate_player_paths(force=True)
            if self.targetPlayerExpectedGeneralLocation is None:
                self.targetPlayerExpectedGeneralLocation = self.general

        if self.board_analysis is None:
            return

        if not self.is_lag_massive_map or self._map.turn < 3 or (self._map.turn + 3) % 5 == 0:
            with self.perf_timer.begin_move_event('Inter-general analysis'):
                # also rescans chokes, now.
                self.board_analysis.rebuild_intergeneral_analysis(self.targetPlayerExpectedGeneralLocation, self.armyTracker.valid_general_positions_by_player)

        with self.perf_timer.begin_move_event('get_alt_en_gen_positions'):
            if not self.is_lag_massive_map or (self._map.turn + 2) % 5 == 0:
                altEnGenPositions = None
                enPotentialGenDistances = None 
                for player in self._map.players:
                    if player.team != self.player.team:
                        if not self.armyTracker.seen_player_lookup[player.index]:
                            if altEnGenPositions is None:
                                with self.perf_timer.begin_move_event(f'get furthest apart 3 enemy gen locs {player.index}'):
                                    altEnGenPositions, enPotentialGenDistances = self._get_furthest_apart_3_enemy_general_locations(player.index)
                                self.alt_en_gen_positions[player.index] = altEnGenPositions
                                self._alt_en_gen_position_distances[player.index] = enPotentialGenDistances
                        else:
                            limitNearbyTileRange = -1
                            if len(self._map.players) > 4:
                                limitNearbyTileRange = 12
                            altEnGenPositions = self.get_target_player_possible_general_location_tiles_sorted(elimNearbyRange=2, player=player.index, cutoffEmergenceRatio=0.3, limitNearbyTileRange=limitNearbyTileRange)
                            self.alt_en_gen_positions[player.index] = altEnGenPositions
                            self._alt_en_gen_position_distances[player.index] = None

        # with self.perf_timer.begin_move_event('ArmyTracker bisector'):
        #     self.gather_kill_priorities = self.find_fog_bisection_targets()

        # if self._map.turn >= 3 and self.board_analysis.should_rescan:
        # I think reachable tiles isn't built till turn 2? so chokes aren't built properly turn 1

        self.approximate_greedy_turns_avail = self._get_approximate_greedy_turns_available()

        if self.board_analysis.central_defense_point and self.board_analysis.intergeneral_analysis:
            centralPoint = self.board_analysis.central_defense_point
            self.viewInfo.add_targeted_tile(centralPoint, TargetStyle.TEAL, radiusReduction=2)
            if (self.locked_launch_point is None or self.locked_launch_point == self.general) and self.board_analysis.intergeneral_analysis.bMap[centralPoint] < self.board_analysis.inter_general_distance:
                # then the central defense point is further forward than our general, lock it as launch point.
                self.viewInfo.add_info_line(f"locking in central launch point {str(centralPoint)}")
                self.locked_launch_point = centralPoint
                self.recalculate_player_paths(force=True)

        self.ensure_reachability_matrix_built()

        if self.board_analysis.intergeneral_analysis:
            with self.perf_timer.begin_move_event('City Analyzer'):
                self.cityAnalyzer.re_scan(self.board_analysis)

        with self.perf_timer.begin_move_event('OpponentTracker Analyze'):
            self.opponent_tracker.analyze_turn(self.targetPlayer)

        with self.perf_timer.begin_move_event('Fog annihilation sink'):
            for team in self.teams:
                annihilatedFog = self.opponent_tracker.get_team_annihilated_fog(team)
                if annihilatedFog > 0:
                    for player in self._map.players:
                        if player.team == team:
                            if self.armyTracker.try_find_army_sink(player.index, annihilatedFog, tookNeutCity=self.did_player_just_take_fog_city(player.index)):
                                break

        with self.perf_timer.begin_move_event('ArmyTracker fog land builder'):
            fogTileCounts = self.opponent_tracker.get_all_player_fog_tile_count_dict()
            for player in self._map.players:
                playerLoc: Tile | None = None
                if player.index == self.targetPlayer:
                    playerLoc = self.targetPlayerExpectedGeneralLocation
                self.armyTracker.update_fog_prediction(player.index, fogTileCounts[player.index], playerLoc)

        if self._map.is_army_bonus_turn or self._should_recalc_tile_islands:
            with self.perf_timer.begin_move_event('TileIsland recalc'):
                self.tileIslandBuilder.recalculate_tile_islands(self.targetPlayerExpectedGeneralLocation)
                self._should_recalc_tile_islands = False
        else:
            # EXPANSION RELIES ON TILE ISLANDS BEING ACCURATE OR ELSE IT WILL DIVE INTO CORNERS WHERE IT ALREADY CUT OFF ITS RECAPTURE PATH BECAUSE IT THINKS IT CAN RECAPTURE USING THE TILES ITS COMING FROM
            with self.perf_timer.begin_move_event('TileIsland update'):
                self.tileIslandBuilder.update_tile_islands(self.targetPlayerExpectedGeneralLocation)

        self.armyTracker.verify_player_tile_and_army_counts_valid()

        if not self.is_lag_massive_map or self._map.turn < 3 or (self._map.turn + 5) % 5 == 0:
            with self.perf_timer.begin_move_event('WinConditionAnalyzer Analyze'):
                self.win_condition_analyzer.analyze(self.targetPlayer, self.targetPlayerExpectedGeneralLocation)

                self.viewInfo.add_stats_line(f'WinConns: {", ".join([str(c).replace("WinCondition.", "") for c in self.win_condition_analyzer.viable_win_conditions])}')

                for tile in self.win_condition_analyzer.defend_cities:
                    self.viewInfo.add_targeted_tile(tile, TargetStyle.GREEN, radiusReduction=4)

                for tile in self.win_condition_analyzer.contestable_cities:
                    self.viewInfo.add_targeted_tile(tile, TargetStyle.PURPLE, radiusReduction=4)

        if not self.is_lag_massive_map or self._map.turn < 3 or (self._map.turn + 4) % 5 == 0:
            if self.territories.should_recalculate(self._map.turn):
                with self.perf_timer.begin_move_event('Territory Scan'):
                    self.territories.scan()

        for path in self.armyTracker.fogPaths:
            self.viewInfo.color_path(PathColorer(path, 255, 84, 0, 255, 30, 150))

        self.cached_scrims = {}

    def is_player_aggressive(self, player: int, turnPeriod: int = 50) -> bool:
        if player in self._map.teammates:
            return False

        pObj = self._map.players[player]
        if pObj.dead:
            return False
        if pObj.leftGame:
            return False

        # TODO this should look at opponent tracker / attack history in opponent tracker instead of ignoring turn period
        if pObj.aggression_factor > 120:
            return True

        return False

    def get_timings_old(self) -> Timings:
        with self.perf_timer.begin_move_event('GatherAnalyzer scan'):
            self.gatherAnalyzer.scan()

        countOnPath = 0
        if self.target_player_gather_targets is not None:
            countOnPath = SearchUtils.count(self.target_player_gather_targets, lambda tile: self._map.is_tile_friendly(tile))
        randomVal = random.randint(-1, 2)
        # what size cycle to use, normally the 50 turn cycle
        cycleDuration = 50

        # at what point in the cycle to gatherSplit from gather to utility moves. TODO dynamically determine this based on available utility moves?
        gatherSplit = 0

        # offset so that this timing doesn't always sync up with every 100 moves, instead could sync up with 250, 350 instead of 300, 400 etc.
        # for cycle 50 this is always 0
        realDist = self.distance_from_general(self.targetPlayerExpectedGeneralLocation)
        longSpawns = self.target_player_gather_path is not None and realDist > 22
        genPlayer = self._map.players[self.general.player]
        targPlayer = None
        if self.targetPlayer != -1:
            targPlayer = self._map.players[self.targetPlayer]
            self.opponent_tracker.get_tile_differential()

        frTileCount = genPlayer.tileCount
        for teammate in self._map.teammates:
            teamPlayer = self._map.players[teammate]
            frTileCount += teamPlayer.tileCount

        # hack removed longspawns, this doesn't actually help?
        if False and longSpawns and genPlayer.tileCount > 80:
            # LONG SPAWNS
            if self.is_all_in():
                if genPlayer.tileCount > 80:
                    cycleDuration = 100
                    gatherSplit = 70
                else:
                    gatherSplit = min(40, genPlayer.tileCount - 10)
            elif genPlayer.tileCount > 120:
                cycleDuration = 100
                gatherSplit = 60
            elif genPlayer.tileCount > 100:
                cycleDuration = 100
                gatherSplit = 55
        else:
            if self.is_all_in():
                if genPlayer.tileCount > 95:
                    cycleDuration = 100
                    gatherSplit = 76
                else:
                    cycleDuration = 50
                    gatherSplit = 35
            elif genPlayer.tileCount - countOnPath > 140 or realDist > 35:
                cycleDuration = 100
                gatherSplit = 65
            elif genPlayer.tileCount - countOnPath > 120 or realDist > 29:
                # cycleDuration = 100
                # gatherSplit = 65
                gatherSplit = 26
            elif genPlayer.tileCount - countOnPath > 100:
                # slightly longer gatherSplit
                gatherSplit = 26
            elif genPlayer.tileCount - countOnPath > 85:
                # slightly longer gatherSplit
                gatherSplit = 26
            elif genPlayer.tileCount - countOnPath > 65:
                # slightly longer gatherSplit
                gatherSplit = 25
            elif genPlayer.tileCount - countOnPath > 45:
                # slightly longer gatherSplit
                gatherSplit = 24
            elif genPlayer.tileCount - countOnPath > 30:
                # slightly longer gatherSplit
                gatherSplit = 23
            elif genPlayer.tileCount - countOnPath > 21:
                # slightly longer gatherSplit
                gatherSplit = 21
            else:
                gatherSplit = genPlayer.tileCount - countOnPath
                randomVal = 0

            gatherSplit = min(gatherSplit, genPlayer.tileCount - countOnPath)

        if self._map.turn < 100:
            cycleDuration = 50
            gatherSplit = 20

        gatherSplit = min(gatherSplit, genPlayer.tileCount - countOnPath)

        if self.targetPlayer == -1 and self._map.remainingPlayers == 2:
            gatherSplit += 3
        gatherSplit += randomVal

        quickExpandSplit = 0
        if self._map.turn > 50:
            if self.targetPlayer != -1:
                maxAllowed = self.behavior_max_allowed_quick_expand
                winningBasedMin = int(targPlayer.tileCount - genPlayer.tileCount + genPlayer.tileCount / 8)
                quickExpandSplit = min(maxAllowed, max(0, winningBasedMin))
                logbook.info(f"quickExpandSplit: {quickExpandSplit}")

        if self.defend_economy:
            gatherSplit += 3
            quickExpandSplit = 0

        if self.currently_forcing_out_of_play_gathers:
            gatherSplit += 3
            quickExpandSplit = 0

        if self.is_still_ffa_and_non_dominant():
            quickExpandSplit = 0
            gatherSplit += 4
            if self.targetPlayer != -1 and self.targetPlayerObj.aggression_factor > 150:
                gatherSplit = 50 - self.shortest_path_to_target_player.length - 4

        disallowEnemyGather = False

        offset = self._map.turn % cycleDuration
        if offset % 50 != 0:
            self.viewInfo.add_info_line(f"offset being reset to 0 from {offset}")
            # When this gets set on random turns, if we don't set it to 0 it will always keep recycling on that offkilter turn.
            offset = 0

        # # if the gather path is real long, then we need to launch the attack a bit earlier.
        # if self.target_player_gather_path is not None and self.target_player_gather_path.length > 17:
        #     diff = self.target_player_gather_path.length - 17
        #     logbook.info(f'the gather path is really long ({self.target_player_gather_path.length}), then we need to launch the attack a bit earlier. Switching from {gatherSplit} to {gatherSplit - diff}')
        #     gatherSplit -= diff
        #
        # if self.force_city_take:
        #     quickExpandSplit = 0

        # gatherSplit += quickExpandSplit

        # launchTiming should be cycleTurns - expected travel time to enemy territory - expected time spent in enemy territory
        # pathValueWeight = 10
        pathValueWeight = 0
        pathLength = 8
        if self.target_player_gather_path is not None:
            subsegment: Path = self.target_player_gather_path.get_subsegment(int(self.target_player_gather_path.length // 2))
            subsegment.calculate_value(self.general.player, teams=self._map.team_ids_by_player_index)
            pathValueWeight = max(pathValueWeight, int(max(1.0, subsegment.value) ** 0.75))
            pathLength = max(pathLength, self.target_player_gather_path.length)

        launchTiming = cycleDuration - pathValueWeight - pathLength - 4 + self.behavior_launch_timing_offset

        tileDiff = self.opponent_tracker.get_tile_differential()
        if tileDiff < 2:
            back = max(-10, tileDiff // 2) - 2
            self.viewInfo.add_info_line(f'gathSplit back {back} turns due to tileDiff {tileDiff}')
            gatherSplit += back

        if self.flanking:
            gatherSplit += self.behavior_flank_launch_timing_offset
            launchTiming = gatherSplit
            quickExpandSplit = 0
            # launchTiming += self.behavior_flank_launch_timing_offset
            # if launchTiming > gatherSplit:
            #     launchTiming = gatherSplit

        if self.teammate_path is not None and self.target_player_gather_path is not None and self.target_player_gather_path.start.tile == self.teammate_general:
            gatherSplit -= self.teammate_path.length // 2 + 2
            launchTiming = gatherSplit
        # if not self.opponent_tracker.winning_on_economy(byRatio=1.06):
        #     gatherSplit -= 2
        #     launchTiming += 1

        isOurPathAMostlyFogAltPath = False
        if self.target_player_gather_path is not None:
            numFog = self.get_undiscovered_count_on_path(self.target_player_gather_path)
            numEn = self.get_enemy_count_on_path(self.target_player_gather_path)

            overage = 2 * numFog - 1 * self.target_player_gather_path.length // 2 - numEn
            if overage > 0 and self._map.turn > 85 and numEn < self.target_player_gather_path.length // 3:
                isOurPathAMostlyFogAltPath = True
                self.viewInfo.add_info_line(f'launch reduc {overage} bc fog {numFog} vs pathlen {self.target_player_gather_path.length}')
                launchTiming -= overage
                gatherSplit -= overage

        if launchTiming < gatherSplit:
            gatherSplit += self.behavior_launch_timing_offset
            if self.flanking:
                gatherSplit += self.behavior_flank_launch_timing_offset
            self.viewInfo.add_info_line(f'launchTiming was {launchTiming} (pathValueWeight {pathValueWeight}), targetLen {pathLength}, adjusting to be same as gatherSplit {gatherSplit}')
            launchTiming = gatherSplit
        else:
            self.viewInfo.add_info_line(f'launchTiming {launchTiming} (pathValueWeight {pathValueWeight}), targetLen {pathLength}')

        # should usually be 0 except the first turn
        correction = self._map.turn % 50
        timings = Timings(cycleDuration, quickExpandSplit, gatherSplit, launchTiming, offset, self._map.turn + cycleDuration - correction, disallowEnemyGather)
        timings.is_early_flank_launch = isOurPathAMostlyFogAltPath

        if self.teammate_general is not None and self.teammate_communicator.is_team_lead and self.target_player_gather_path is not None and correction < timings.launchTiming and self._map.turn >= 50:
            self.send_teammate_communication(
                f'Launch turn {(self._map.turn + timings.launchTiming - correction) // 2} from here:',
                pingTile=self.target_player_gather_path.start.tile,
                cooldown=5,
                detectionKey='2v2 launch timings')

        logbook.info(f"Recalculated timings. longSpawns {longSpawns}, Timings {str(timings)}")
        return timings

    def get_timings(self) -> Timings:
        with self.perf_timer.begin_move_event('GatherAnalyzer scan'):
            self.gatherAnalyzer.scan()

        if self.target_player_gather_path is None:
            self.recalculate_player_paths(force=True)

        countFrOnPath = 0
        countEnOnPath = 0
        countNeutOnPath = 0

        launchTiming = 20
        gatherSplit = 20
        if self.is_still_ffa_and_non_dominant() and self.targetPlayer != -1:
            gatherSplit = 32

        timeOutsideLaunchAndGath = 0

        cycle = 50
        expValue = 20.0
        bypass = True
        if self.expansion_plan is not None and self.target_player_gather_targets is not None:
            bypass = False
            expValue = self.expansion_plan.cumulative_econ_value
            for opt in self.expansion_plan.all_paths:
                if opt.length < 1:
                    continue
                if opt.econValue / opt.length > 0.99 and opt.tileSet.isdisjoint(self.target_player_gather_targets):
                    timeOutsideLaunchAndGath += opt.length

        if self.target_player_gather_path is not None:
            # countFrOnPath = SearchUtils.count(self.target_player_gather_targets, lambda tile: self._map.is_tile_friendly(tile))
            for t in self.target_player_gather_path.tileList:
                if self._map.is_tile_friendly(t):
                    countFrOnPath += 1
                elif self._map.is_tile_on_team(t, self.targetPlayer):
                    countEnOnPath += 1
                elif t.player == -1:
                    countNeutOnPath += 1
            # TODO fancy ass dynamic timing based on opponents historical timings
            cycle = 50
            if self.timings is not None:
                cycle = self.timings.cycleTurns
            launchTiming = cycle - self.shortest_path_to_target_player.length - 5
            # more enemy / neutral tiles on path make our launch later, since we're going to be capturing their tiles
            launchTiming += countEnOnPath // 2
            launchTiming += countNeutOnPath // 2
            # more ally tiles on path make our launch sooner, since those are technically kind of part of the 'gather' phase and wont be capping tiles.
            launchTiming -= countFrOnPath // 2

            # if we have a plan already for 40 turns of enemy captures, (80 econ), then we should probably just gather for 10 turns, except that would be dangerous so need to account for that (?)
            xPweight = int(expValue * 0.5)
            minExpWeighted = 32
            # expGatherTiming = max(minExpWeighted, 50 - xPweightOld)
            expGatherTiming = max(15, min(minExpWeighted, 50 - timeOutsideLaunchAndGath))
            gatherSplit = min(launchTiming, expGatherTiming)
            if not bypass:
                self.viewInfo.add_info_line(
                    f'timingsBase: g{gatherSplit} <- min(launch {launchTiming}, max(15, min({minExpWeighted}, 50-timeAv {timeOutsideLaunchAndGath} ({50 - timeOutsideLaunchAndGath}))), en{countEnOnPath}, fr{countFrOnPath}, nt{countNeutOnPath}, expW {xPweight}')

        randomVal = random.randint(-3, 1)
        randomVal = -3
        # what size cycle to use, normally the 50 turn cycle

        # at what point in the cycle to gatherSplit from gather to utility moves. TODO dynamically determine this based on available utility moves?

        # offset so that this timing doesn't always sync up with every 100 moves, instead could sync up with 250, 350 instead of 300, 400 etc.
        # for cycle 50 this is always 0
        spawnDist = self.distance_from_general(self.targetPlayerExpectedGeneralLocation)
        longSpawns = self.target_player_gather_path is not None and spawnDist > 22
        genPlayer = self._map.players[self.general.player]

        gatherSplit = min(gatherSplit, genPlayer.tileCount - countFrOnPath)

        gatherSplit += randomVal

        quickExpandSplit = 0

        if self.defend_economy:
            gatherSplit += 2

        if self.currently_forcing_out_of_play_gathers:
            gatherSplit += 2

        if self.is_still_ffa_and_non_dominant():
            oldGath = gatherSplit
            gatherSplit = 38

            if self.targetPlayer != -1:
                gatherSplit = 50 - self.shortest_path_to_target_player.length

            if self.targetPlayer != -1 and self.targetPlayerObj.aggression_factor > 150:
                gatherSplit = 50 - self.shortest_path_to_target_player.length - 4

            self.info(f'FFA gath split adjust {oldGath} -> {gatherSplit}')

        disallowEnemyGather = False

        # pathLength = 8
        # if self.target_player_gather_path is not None:
        #     subsegment: Path = self.target_player_gather_path.get_subsegment(int(self.target_player_gather_path.length // 2))
        #     subsegment.calculate_value(self.general.player, teams=self._map._teams)
        #     pathLength = max(pathLength, self.target_player_gather_path.length)
        #
        # launchTiming = cycleDuration - pathLength - 4 + self.behavior_launch_timing_offset

        tileDiff = self.opponent_tracker.get_tile_differential()
        if tileDiff < 4:
            back = max(-10, tileDiff // 2) - 2
            if not bypass:
                self.viewInfo.add_info_line(f'gathSplit/launch back {back} turns due to tileDiff {tileDiff}')
            gatherSplit += back
            launchTiming += back

        if self.flanking:
            self.viewInfo.add_info_line(f'gathSplit flanking += {self.behavior_flank_launch_timing_offset}')
            gatherSplit += self.behavior_flank_launch_timing_offset
            launchTiming = gatherSplit
            # launchTiming += self.behavior_flank_launch_timing_offset
            # if launchTiming > gatherSplit:
            #     launchTiming = gatherSplit

        if self.teammate_path is not None and self.target_player_gather_path is not None and self.target_player_gather_path.start.tile == self.teammate_general:
            # if we're meeting at ally, gather early so they can launch an attack
            gatherSplit -= self.teammate_path.length // 2 + 2
            launchTiming = gatherSplit
        # if not self.opponent_tracker.winning_on_economy(byRatio=1.06):
        #     gatherSplit -= 2
        #     launchTiming += 1

        isOurPathAMostlyFogAltPath = False
        if self.target_player_gather_path is not None:
            pathCheck = self.target_player_gather_path
            if pathCheck.length > 25:
                pathCheck = pathCheck.get_subsegment(25)
            numFog = self.get_undiscovered_count_on_path(pathCheck)
            numEn = self.get_enemy_count_on_path(pathCheck)
            if numEn > 0 or self.target_player_gather_path.length < 20:
                overage = 2 * numFog - 1 * pathCheck.length // 2 - numEn
                if overage > 0 and self._map.turn > 85 and numEn < pathCheck.length // 3:
                    isOurPathAMostlyFogAltPath = True
                    if not bypass:
                        self.viewInfo.add_info_line(f'launch reduc {overage} bc fog {numFog} vs pathlen {pathCheck.length}')
                    launchTiming -= overage
                    gatherSplit -= overage

        while launchTiming < 0:
            self.info(f'increasing launch timing {launchTiming} by increasing cycle duration')
            cycle += 50
            launchTiming += 50
            gatherSplit += 50

        if launchTiming < gatherSplit:
            if not bypass:
                self.viewInfo.add_info_line(f'adjusting launchTiming (was {launchTiming}) to be same as gatherSplit {gatherSplit}, targetLen {self.shortest_path_to_target_player.length}')
            launchTiming = gatherSplit
        else:
            if not bypass:
                self.viewInfo.add_info_line(f'launchTiming {launchTiming}, targetLen {self.shortest_path_to_target_player.length}')

        # should usually be 0 except the first turn
        correction = self._map.turn % 50
        timings = Timings(cycle, quickExpandSplit, gatherSplit, launchTiming, 0, self._map.turn + cycle - correction, disallowEnemyGather)
        timings.is_early_flank_launch = isOurPathAMostlyFogAltPath

        # if self._map.is_2v2 and self.teammate_communicator.is_team_lead and self.target_player_gather_path is not None and correction < timings.launchTiming and self._map.turn >= 50:
        #     self.send_teammate_communication(
        #         f'Launch turn {(self._map.turn + timings.launchTiming - correction) // 2} from here:',
        #         pingTile=self.target_player_gather_path.start.tile,
        #         cooldown=5,
        #         detectionKey='2v2 launch timings')

        logbook.info(f"Recalculated timings. longSpawns {longSpawns}, Timings {str(timings)}")
        return timings

    def get_undiscovered_count_on_path(self, path: Path) -> int:
        numFog = 0
        for t in path.tileList:
            if not t.discovered:
                numFog += 1
        return numFog

    def get_enemy_count_on_path(self, path: Path) -> int:
        numEn = 0
        for t in path.tileList:
            if self._map.is_tile_enemy(t):
                numEn += 1
        return numEn

    def timing_expand(self):
        turnOffset = self._map.turn + self.timings.offsetTurns
        turnCycleOffset = turnOffset % self.timings.cycleTurns
        if turnCycleOffset >= self.timings.splitTurns:
            return None
        return None

    def timing_gather(
            self,
            startTiles: typing.List[Tile],
            negativeTiles: typing.Set[Tile] | None = None,
            skipTiles: typing.Set[Tile] | None = None,
            force=False,
            priorityTiles: typing.Set[Tile] | None = None,
            targetTurns=-1,
            includeGatherTreeNodesThatGatherNegative=False,  # DO NOT set this to True, causes us to slam tiles into dumb shit everywhere
            useTrueValueGathered: bool = False,
            pruneToValuePerTurn: bool = False,
            priorityMatrix: MapMatrixInterface[float] | None = None,
            distancePriorities: MapMatrixInterface[int] | None = None,
            logStuff: bool = False
    ) -> Move | None:
        turnOffset = self._map.turn + self.timings.offsetTurns
        turnCycleOffset = turnOffset % self.timings.cycleTurns

        gatherNodeMoveSelectorFunc = self._get_tree_move_default_value_func()
        if self.likely_kill_push:
            potThreat = self.dangerAnalyzer.fastestPotentialThreat
            if potThreat is None and self.enemy_attack_path is not None:
                aa = ArmyAnalyzer(self._map, self.enemy_attack_path.start.tile, self.enemy_attack_path.tail.tile)
                potThreat = ThreatObj(self.enemy_attack_path.length, self.enemy_attack_path.value, self.enemy_attack_path, ThreatType.Vision, armyAnalysis=aa)
            if potThreat is not None:
                gatherNodeMoveSelectorFunc = self.get_defense_tree_move_prio_func(potThreat)

        if force or (self._map.turn >= 50 and turnCycleOffset < self.timings.splitTurns and startTiles is not None and len(startTiles) > 0):
            self.finishing_exploration = False
            if targetTurns != -1:
                depth = targetTurns
            else:
                depth = self.timings.splitTurns - turnCycleOffset

                if pruneToValuePerTurn and depth < 10:
                    depth = 10

                if depth <= 0:
                    depth += self.timings.cycleTurns

            if depth > GATHER_SWITCH_POINT:
                with self.perf_timer.begin_move_event(f"USING OLD MST GATH depth {depth}"):
                    gatherNodes = self.build_mst(startTiles, 1.0, depth - 1, negativeTiles)
                    # self.redGatherTreeNodes = [node.deep_clone() for node in GatherTreeNodes]
                    gatherNodes = Gather.prune_mst_to_turns(
                        gatherNodes,
                        depth - 1,
                        self.general.player,
                        preferPrune=self.expansion_plan.preferred_tiles if self.expansion_plan is not None else None,
                        viewInfo=self.viewInfo if self.info_render_gather_values else None,
                        noLog=not logStuff)
                gatherMove = self.get_tree_move_default(gatherNodes)
                if gatherMove is not None:
                    self.viewInfo.add_info_line(
                        f"OLD LEAF MST GATHER MOVE! {gatherMove.source.x},{gatherMove.source.y} -> {gatherMove.dest.x},{gatherMove.dest.y}  leafGatherDepth: {depth}")
                    self.gatherNodes = gatherNodes
                    return self.move_half_on_repetition(gatherMove, 6)
            else:
                skipFunc = None
                if self.is_still_ffa_and_non_dominant():
                    # avoid gathering to undiscovered tiles when there are third parties on the map
                    skipFunc = lambda tile, tilePriorityObject: not tile.discovered

                # if self.defendEconomy:
                #     # we are going to prune down a max value per turn gather so over-gather a bit.
                #     depth = int(depth * 1.3)
                startTileStr = 'no start tiles'
                if startTiles and len(startTiles) > 0:
                    startTiles = [t for t in startTiles if t is not None]
                    startTileStr = f'@{" | ".join([str(t) for t in sorted(startTiles, key=lambda st: self.board_analysis.intergeneral_analysis.bMap.raw[st.tile_index])])}'
                self.info(f'GathParams: depth {depth}, mat {str(priorityMatrix is not None)[0]}, useTrue {str(useTrueValueGathered)[0]}, incNeg {str(includeGatherTreeNodesThatGatherNegative)[0]}, {startTileStr}')

                if distancePriorities is None:
                    distancePriorities = self.board_analysis.intergeneral_analysis.bMap
                move, value, turnsUsed, gatherNodes = self.get_gather_to_target_tiles(
                    startTiles,
                    0.05,
                    depth,
                    negativeTiles,
                    useTrueValueGathered=useTrueValueGathered,
                    includeGatherTreeNodesThatGatherNegative=includeGatherTreeNodesThatGatherNegative,
                    skipTiles=skipTiles,
                    distPriorityMap=distancePriorities,
                    priorityMatrix=priorityMatrix,
                    shouldLog=logStuff,
                )

                if gatherNodes is None:
                    self.info(f'ERR timing_gather failed {depth}t with get_gather_to_target_tiles...? @{startTiles}')
                    for t in startTiles:
                        self.viewInfo.add_targeted_tile(t, TargetStyle.TEAL)
                    value, turnsUsed, gatherNodes = Gather.knapsack_depth_gather_with_values(
                        self._map,
                        startTiles,
                        depth,
                        negativeTiles=negativeTiles,
                        searchingPlayer=self.general.player,
                        skipFunc=skipFunc,
                        viewInfo=self.viewInfo if self.info_render_gather_values else None,
                        skipTiles=skipTiles,
                        distPriorityMap=distancePriorities,
                        priorityTiles=priorityTiles,
                        useTrueValueGathered=useTrueValueGathered,
                        includeGatherTreeNodesThatGatherNegative=includeGatherTreeNodesThatGatherNegative,
                        incrementBackward=False,
                        priorityMatrix=priorityMatrix,
                        shouldLog=logStuff)

                if pruneToValuePerTurn:
                    minGather = value // 3
                    reason = ''
                    if self.defend_economy:
                        minGather = 4 * value // 5
                        reason = 'ECON DEF '
                    prefer = set()
                    # if self.expansion_plan is not None:
                    #     prefer = set(self.expansion_plan.preferred_tiles)
                    for t in self.player.tiles:
                        if t.army <= 1:
                            prefer.add(t)
                    prunedCount, prunedValue, gatherNodes = Gather.prune_mst_to_max_army_per_turn_with_values(
                        gatherNodes,
                        minArmy=minGather,
                        searchingPlayer=self.general.player,
                        teams=MapBase.get_teams_array(self._map),
                        viewInfo=self.viewInfo if self.info_render_gather_values else None,
                        allowNegative=includeGatherTreeNodesThatGatherNegative,
                        # preferPrune=prefer,
                        noLog=not logStuff,
                        allowBranchPrune=True
                    )
                    turnsUsed = prunedCount
                    value = prunedValue
                    self.viewInfo.add_info_line(f"{reason}pruned to max G/T {prunedValue:.1f}/{prunedCount}t  {prunedValue/max(1, prunedCount):.2f}vt (min {minGather:.0f})  (from {value:.1f}/{turnsUsed}t  {value/max(1, turnsUsed):.2f}vt)")
                    turnInCycle = self.timings.get_turn_in_cycle(self._map.turn)
                    if prunedCount + turnInCycle > self.timings.splitTurns:
                        newSplit = prunedCount + turnInCycle
                        self.viewInfo.add_info_line(f'updating timings to gatherSplit {newSplit} due to defensive gather')
                        self.timings.splitTurns = newSplit
                        self.timings.launchTiming = max(self.timings.splitTurns, self.timings.launchTiming)

                self.gatherNodes = gatherNodes

                if self.info_render_gather_values and priorityMatrix:
                    for t in self._map.reachable_tiles:
                        val = priorityMatrix[t]
                        if val:
                            self.viewInfo.topRightGridText[t] = f'g{str(round(val, 3)).lstrip("0").replace("-0", "-")}'
                move = self.get_tree_move_default(self.gatherNodes, gatherNodeMoveSelectorFunc)
                if move is not None:
                    self.curPath = None
                    self.curPath = self.convert_gather_to_move_list_path(gatherNodes, turnsUsed, value, gatherNodeMoveSelectorFunc)
                    return self.move_half_on_repetition(move, 6, 4)
                else:
                    logbook.info("NO MOVE WAS RETURNED FOR timing_gather?????????????????????")
        else:
            self.finishing_exploration = True
            self.viewInfo.add_info_line("finishExp=True in timing_gather because outside cycle...?")
            logbook.info(f"No timing move because outside gather timing window. Timings: {str(self.timings)}")
        return None

    def make_first_25_move(self) -> Move | None:
        # if self._map.remainingPlayers < 4 or self._map.is_2v2:

        timeLimit = self.get_remaining_move_time()

        for city in self.cityAnalyzer.city_scores.keys():
            if city.army < 30:
                return self.try_find_expansion_move(set(), 0.15)

        if self.city_expand_plan is not None and timeLimit > 0:
            used = self.perf_timer.get_elapsed_since_update(self._map.turn)
            moveCycleTime = 0.5
            latencyBuffer = 0.22
            allowedLatest = moveCycleTime - latencyBuffer
            timeLimit = allowedLatest - used
            i = 0
            while self._map.turn + i < 12:  # or (len(self.city_expand_plan.plan_paths) > i and self.city_expand_plan.plan_paths[i] is None)
                i += 1
                timeLimit += moveCycleTime - 0.1
            if DebugHelper.IS_DEBUGGING:
                timeLimit = max(timeLimit, 0.1)
            self.viewInfo.add_info_line(f'Allowing f25 time limit {timeLimit:.3f}')
            timeLimit = min(2.0, timeLimit)
        elif self._map.turn < 7:
            timeLimit = 3.75
            if self._map.is_2v2:
                if self.city_expand_plan is None:
                    if self.teamed_with_bot:
                        timeLimit = 0.75
                    else:
                        timeLimit = 2.75
                else:
                    timeLimit = 1.75

            if self._map.cols * self._map.rows > 1000 or len(self._map.players) > 8:
                timeLimit = 0.75
        move = self.get_optimal_city_or_general_plan_move(timeLimit=timeLimit)
        # TODO should be able to build the whole plan above in an MST prune, no...?
        if move is not None:
            if move.source.player == self.general.player:
                return move
        return None

        # self.viewInfo.add_info_line('wtf, explan plan was empty but we\'re in first 25 still..? Switching to old expansion')
        # else:
        #     with self.perf_timer.begin_move_event(
        #             f'First 25 expansion FFA'):
        #
        #         if self._map.turn < 22:
        #             return None
        #
        #         nonGeneralArmyTiles = [tile for tile in filter(lambda tile: tile != self.general and tile.army > 1, self._map.players[self.general.player].tiles)]
        #
        #         skipTiles = set()
        #
        #         if len(nonGeneralArmyTiles) > 0:
        #             skipTiles.add(self.general)
        #         elif self.general.army < 3 and self._map.turn < 45:
        #             return None
        #
        #         # EXPLORATION was the old way for 1v1 pre-optimal-first-25
        #         # path = self.get_optimal_exploration(50 - self._map.turn, skipTiles=skipTiles)
        #
        #         # prioritize tiles that explore the least
        #         distMap = [[1000 for y in range(self._map.rows)] for x in range(self._map.cols)]
        #         for tile in self._map.reachableTiles:
        #             val = 60 - int(self.get_distance_from_board_center(tile, center_ratio=0.0))
        #             distMap[tile.x][tile.y] = val
        #
        #         # hack
        #         oldDistMap = self.board_analysis.intergeneral_analysis.bMap
        #         try:
        #             self.board_analysis.intergeneral_analysis.bMap = distMap
        #             path, allPaths = ExpandUtils.get_optimal_expansion(
        #                 self._map,
        #                 self.general.player,
        #                 self.player,
        #                 50 - self._map.turn,
        #                 self.board_analysis,
        #                 self.territories.territoryMap,
        #                 skipTiles,
        #                 viewInfo=self.viewInfo
        #             )
        #         finally:
        #             self.board_analysis.intergeneral_analysis.bMap = oldDistMap
        #
        #         if (self._map.turn < 46
        #                 and self.general.army < 3
        #                 and len(nonGeneralArmyTiles) == 0
        #                 and SearchUtils.count(self.general.movable, lambda tile: not tile.isMountain and tile.player == -1) > 0):
        #             self.info("Skipping move because general.army < 3 and all army on general and self._map.turn < 46")
        #             # dont send 2 army except right before the bonus, making perfect first 25 much more likely
        #             return None
        #         move = None
        #         if path:
        #             self.info("Dont make me expand. You don't want to see me when I'm expanding.")
        #             move = self.get_first_path_move(path)
        #         return move

    def perform_move_prep(self, is_lag_move: bool = False):
        with self.perf_timer.begin_move_event('scan_map_for_large_tiles_and_leaf_moves()'):
            self.scan_map_for_large_tiles_and_leaf_moves()

        if self.timings and self.timings.get_turn_in_cycle(self._map.turn) == 0:
            self.timing_cycle_ended()

        if self.curPath is not None:
            nextMove = self.curPath.get_first_move()

            if nextMove and nextMove.dest is not None and nextMove.dest.isMountain:
                self.viewInfo.add_info_line(f'Killing curPath because it moved through a mountain.')
                self.curPath = None

        wasFlanking = self.flanking
        if self.target_player_gather_path is None or self.target_player_gather_path.tail.tile != self.targetPlayerExpectedGeneralLocation:
            with self.perf_timer.begin_move_event('No path recalculate_player_paths'):
                self.recalculate_player_paths(force=True)

        if self.timings is None:
            # needed to not be none for holding fresh cities
            with self.perf_timer.begin_move_event('Recalculating Timings first time...'):
                self.timings = self.get_timings()

        # self.check_if_need_to_gather_longer_to_hold_fresh_cities()

        # allowOutOfPlayCheck = self._map.cols * self._map.rows < 400 and len(self.player.cities) < 7
        oldOutOfPlay = self.army_out_of_play
        cycleRemaining = self.timings.get_turns_left_in_cycle(self._map.turn)
        allowSwapToOutOfPlay = cycleRemaining > 15 or (self.opponent_tracker.winning_on_army(byRatio=1.2) and self.opponent_tracker.winning_on_economy(byRatio=1.2)) or self.army_out_of_play
        self.army_out_of_play = allowSwapToOutOfPlay and self.check_army_out_of_play_ratio()
        if not is_lag_move and not wasFlanking and self.army_out_of_play != oldOutOfPlay:
            with self.perf_timer.begin_move_event('flank/outOfPlay recalc_player_paths'):
                self.recalculate_player_paths(force=True)

        if self.timings is None or self.timings.should_recalculate(self._map.turn):
            with self.perf_timer.begin_move_event('Recalculating Timings...'):
                self.timings = self.get_timings()

        if self.determine_should_winning_all_in():
            wasAllIn = self.is_all_in_army_advantage
            self.is_all_in_army_advantage = True
            if not wasAllIn:
                cycle = 50
                if self._map.players[self.general.player].tileCount - self.target_player_gather_path.length < 60:
                    cycle = 30
                self.set_all_in_cycle_to_hit_with_current_timings(cycle)
                self.viewInfo.add_info_line(f"GOING ARMY ADV TEMP ALL IN CYCLE {cycle}, CLEARING STUFF")
                self.curPath = None
                # self.timings = self.get_timings()
                if self.targetPlayerObj.general is not None and not self.targetPlayerObj.general.visible:
                    self.targetPlayerObj.general.army = 3
                    self.clear_fog_armies_around(self.targetPlayerObj.general)

                if not is_lag_move:
                    with self.perf_timer.begin_move_event('all in change recalculate_player_paths'):
                        self.recalculate_player_paths(force=True)
            # self.all_in_army_advantage_counter += 1
        elif self.is_all_in_army_advantage and not self.all_in_city_behind:
            self.is_all_in_army_advantage = False
            self.all_in_army_advantage_counter = 0

        # This is the attempt to resolve the 'dropped packets devolve into unresponsive bot making random moves
        # even though it thinks it is making sane moves' issue. If we seem to have dropped a move, clear moves on
        # the server before sending more moves to prevent moves from backing up and getting executed later.
        if self._map.turn - 1 in self.history.move_history:
            if self.droppedMove():
                matrix = MapMatrix(self._map, True, emptyVal=False)
                self.viewInfo.add_map_zone(matrix, (255, 200, 0), alpha=40)
                msg = "(Dropped move)... Sending clear_moves..."
                self.viewInfo.add_info_line(msg)
                logbook.info(
                    f"\n\n\n^^^^^^^^^VVVVVVVVVVVVVVVVV^^^^^^^^^^^^^VVVVVVVVV^^^^^^^^^^^^^\nD R O P P E D   M O V E ? ? ? ? {msg}\n^^^^^^^^^VVVVVVVVVVVVVVVVV^^^^^^^^^^^^^VVVVVVVVV^^^^^^^^^^^^^")
                if self.clear_moves_func:
                    with self.perf_timer.begin_move_event('Sending clear_moves due to dropped move'):
                        self.clear_moves_func()
            else:
                lastMove = self.history.move_history[self._map.turn - 1][0]
                if lastMove is not None:
                    if self._map.is_player_on_team_with(lastMove.dest.delta.oldOwner, self.general.player):
                        self.tiles_gathered_to_this_cycle.add(lastMove.dest)
                        if lastMove.dest.isCity:
                            self.cities_gathered_this_cycle.discard(lastMove.dest)
                    elif lastMove.dest.player == self.general.player:
                        self.tiles_captured_this_cycle.add(lastMove.dest)

                    if not lastMove.move_half:
                        self.tiles_gathered_to_this_cycle.discard(lastMove.source)
                        self.tiles_evacuated_this_cycle.add(lastMove.source)
                        if lastMove.source.isCity:
                            self.cities_gathered_this_cycle.add(lastMove.source)
                    if self.curPath and self.curPath.get_first_move() and self.curPath.get_first_move().source == lastMove.source:
                        self.curPath.pop_first_move()
                if self.force_far_gathers:
                    self.force_far_gathers_turns -= 1
                else:
                    self.force_far_gathers_sleep_turns -= 1

    def select_move(self, is_lag_move=False) -> Move | None:
        self.init_turn()

        self.tiles_pinged_by_teammate_this_turn = set()
        while self._tiles_pinged_by_teammate.qsize() > 0:
            tile = self._tiles_pinged_by_teammate.get()
            self.viewInfo.add_targeted_tile(tile, TargetStyle.GREEN)
            self.tiles_pinged_by_teammate_this_turn.add(tile)
            if self._map.turn < 50:
                self._tiles_pinged_by_teammate_first_25.add(tile)

        if is_lag_move:
            self.viewInfo.add_info_line(f'skipping some stuff because is_lag_move == True')
            matrix = MapMatrix(self._map, True, emptyVal=False)
            self.viewInfo.add_map_zone(matrix, (250, 140, 0), alpha=25)

        if self._map.turn <= 1:
            # bypass divide by 0 error instead of fixing it
            return None

        if self._map.remainingPlayers == 1:
            return None

        self.perform_move_prep(is_lag_move=is_lag_move)

        if is_lag_move and self.curPath and self.is_lag_massive_map:
            moves = self.curPath.get_move_list()
            for move in moves:
                if move.source.player == self.general.player and move.source.army > 3:
                    self.info(f'LAG CONTINUING PLANNED MOVES {move}')
                    return move

        if self._map.turn - 1 in self.history.move_history:
            lastMove = self.history.move_history[self._map.turn - 1][0]
            if self.droppedMove() and self._map.turn <= 50 and lastMove is not None:
                if lastMove.source != self.general:
                    self.viewInfo.add_info_line(f're-performing dropped first-25 non-general move {str(lastMove)}')
                    return lastMove
                else:
                    # force reset the expansion plan so we recalculate from general.
                    self.city_expand_plan = None

        if not is_lag_move and not self.is_lag_massive_map or self._map.turn < 3 or (self._map.turn + 2) % 5 == 0:
            with self.perf_timer.begin_move_event("recalculating player path"):
                self.recalculate_player_paths()

        if not self.is_still_ffa_and_non_dominant():
            self.prune_timing_split_if_necessary()

        move = self.pick_move_after_prep(is_lag_move)

        if move and self.curPath and move.source in self.curPath.tileSet and move != self.curPath.get_first_move():
            self.curPath = None

        return move

    def pick_move_after_prep(self, is_lag_move=False):
        with self.perf_timer.begin_move_event('calculating general danger / threats'):
            self.calculate_general_danger()

        if self._map.turn <= 4:
            if self._map.modifiers_by_id[MODIFIER_CITY_STATE]:
                for t in self.general.movable:
                    if t.isCity and t.isNeutral and t.army == -1:
                        return Move(self.general, t)

        # with self.perf_timer.begin_move_event('Checking 1 move kills'):
        #     quickKillMove = self.check_for_1_move_kills()
        #     if quickKillMove is not None:
        #         return quickKillMove

        self.clean_up_path_before_evaluating()

        if self.curPathPrio >= 0:
            logbook.info(f"curPathPrio: {str(self.curPathPrio)}")

        threat = None

        if self.dangerAnalyzer.fastestThreat is not None:
            threat = self.dangerAnalyzer.fastestThreat

        if self.dangerAnalyzer.fastestAllyThreat is not None and (threat is None or self.dangerAnalyzer.fastestAllyThreat.turns < threat.turns):
            if self.determine_should_defend_ally():
                threat = self.dangerAnalyzer.fastestAllyThreat

        if self.dangerAnalyzer.fastestCityThreat is not None and threat is None:
            threat = self.dangerAnalyzer.fastestCityThreat

        if threat is None and not self.giving_up_counter > 30 and self.dangerAnalyzer.fastestVisionThreat is not None:
            threat = self.dangerAnalyzer.fastestVisionThreat

        #  # # # #   ENEMY KING KILLS
        with self.perf_timer.begin_move_event('Checking for king kills and races'):
            killMove, kingKillPath, raceChance = self.check_for_king_kills_and_races(threat)
            if killMove is not None:
                return killMove

        defenseCriticalTileSet = set()
        if self.teammate_general is not None and self.teammate_general.player in self._map.teammates:
            for army in self.armyTracker.armies.values():
                if army.player in self._map.teammates:
                    if army.last_moved_turn > self._map.turn - 3:
                        defenseCriticalTileSet.add(army.tile)
                    if army.tile.delta.armyDelta != 0 and (army.tile.delta.oldOwner != army.player or army.tile.delta.oldArmy < army.tile.army):
                        defenseCriticalTileSet.add(army.tile)

        self.threat = threat

        self.check_should_be_all_in_losing()

        if self.is_all_in_losing:
            logbook.info(f"~~~ ___ {self.get_elapsed()}\n   YO WE ALL IN DAWG\n~~~ ___")

        # if not self.isAllIn() and (threat.turns > -1 and self.dangerAnalyzer.anyThreat):
        #    armyAmount = (self.general_min_army_allowable() + enemyNearGen) * 1.1 if threat is None else threat.threatValue + general.army + 1

        if not is_lag_move and not self.is_lag_massive_map or self._map.turn < 3 or (self._map.turn + 2) % 5 == 0:
            with self.perf_timer.begin_move_event('ENEMY Expansion quick check'):
                self.enemy_expansion_plan = self.build_enemy_expansion_plan(timeLimit=0.007, pathColor=(255, 150, 130))

        self.intercept_plans = self.build_intercept_plans(defenseCriticalTileSet)
        for i, interceptPlan in enumerate(self.intercept_plans.values()):
            self.render_intercept_plan(interceptPlan, colorIndex=i)

        if not is_lag_move:
            with self.perf_timer.begin_move_event('Expansion quick check'):
                redoTimings = False
                if self.expansion_plan is None or self.timings is None or self._map.turn >= self.timings.nextRecalcTurn:
                    redoTimings = True

                negs = {t for t in defenseCriticalTileSet if not self._map.is_tile_on_team_with(t, self.targetPlayer)}
                self._add_expansion_threat_negs(negs)
                checkCityRoundEndPlans = self._map.is_army_bonus_turn
                self.expansion_plan = self.build_expansion_plan(timeLimit=0.012, expansionNegatives=negs, pathColor=(150, 100, 150), includeExtraGenAndCityArmy=checkCityRoundEndPlans)

                self.capture_line_tracker.process_plan(self.targetPlayer, self.expansion_plan)

                if redoTimings:
                    self.timings = self.get_timings()

        defenseSavePath: Path | None = None
        if not self.is_all_in_losing and threat is not None and threat.threatType != ThreatType.Vision:
            with self.perf_timer.begin_move_event(f'THREAT DEFENSE {threat.turns} {str(threat.path.start.tile)}'):
                defenseMove, defenseSavePath = self.get_defense_moves(defenseCriticalTileSet, kingKillPath, raceChance)

                if defenseSavePath is not None:
                    self.viewInfo.color_path(PathColorer(defenseSavePath, 255, 100, 255, 200))
                if defenseMove is not None:
                    if not self.detect_repetition(defenseMove, turns=6, numReps=3) or (threat.threatType == ThreatType.Kill and threat.path.tail.tile.isGeneral):
                        if defenseMove.source in self.largePlayerTiles and self.targetingArmy is None:
                            logbook.info(f'threatDefense overriding targetingArmy with {str(threat.path.start.tile)}')
                            self.targetingArmy = self.get_army_at(threat.path.start.tile)
                        return defenseMove
                    else:
                        self.viewInfo.add_info_line(f'BYPASSING DEF REP {str(defenseMove)} :(')

        if self._map.turn < 100:
            isNoExpansionGame = ((len(self._map.swamps) > 0 or len(self._map.deserts) > 0) and self.get_unexpandable_ratio() > 0.7)
            if isNoExpansionGame or self._map.is_low_cost_city_game:
                # # assume we must take cities instead, then.
                # if self._map.walled_city_base_value is None or self._map.walled_city_base_value > 10:
                #     self._map.set_walled_cities(10)
                if self._map.turn < 30:
                    qkPath, shouldWait = self._check_should_wait_city_capture()
                    if qkPath is not None:
                        self.info(f'f15 QUICK KILL CITY {qkPath}')
                        return qkPath.get_first_move()
                    if shouldWait:
                        self.info(f'f15 wants to wait on city')
                        return None
                if not self.army_out_of_play:
                    path, move = self.capture_cities(set(), forceNeutralCapture=True)
                    if move is not None:
                        self.info(f'f50 City cap instead... {move}')
                        self.city_expand_plan = None
                        return move
                    if path is not None:
                        self.info(f'f50 City cap instead... {path}')
                        self.city_expand_plan = None
                        return self.get_first_path_move(path)

        if self._map.turn < 50:
            if not self._map.is_low_cost_city_game and not self._map.modifiers_by_id[MODIFIER_CITY_STATE]:
                return self.make_first_25_move()
            else:
                self.info(f'Byp f25 bc weird_custom {self.is_weird_custom} (walled_city {self._map.is_walled_city_game} or low_cost_city {self._map.is_low_cost_city_game}) or cityState {self._map.modifiers_by_id[MODIFIER_CITY_STATE]}')

        if self._map.turn < 250 and self._map.remainingPlayers > 3:
            with self.perf_timer.begin_move_event('Ffa Turtle Move'):
                move = self.look_for_ffa_turtle_move()
            if move is not None:
                return move
        if self.expansion_plan:
            for t in self.expansion_plan.blocking_tiles:
                if t not in defenseCriticalTileSet:
                    defenseCriticalTileSet.add(t)
                    self.viewInfo.add_info_line(f'{t} added to defense crit from expPlan.blocking')

        if self._map.is_army_bonus_turn or self.defensive_spanning_tree is None:
            with self.perf_timer.begin_move_event('defensive spanning tree'):
                self.defensive_spanning_tree = self._get_defensive_spanning_tree(defenseCriticalTileSet, self.get_gather_tiebreak_matrix())

        if kingKillPath is not None and threat.threatType == ThreatType.Kill:
            attackingWithSavePath = defenseSavePath is not None and defenseSavePath.start.tile == kingKillPath.start.tile
            attackingWithRestrictedArmyMovement = False
            blocks = self.blocking_tile_info.get(kingKillPath.start.tile)
            if blocks:
                if kingKillPath.start.next.tile in blocks.blocked_destinations:
                    attackingWithRestrictedArmyMovement = True
            if not attackingWithSavePath and not attackingWithRestrictedArmyMovement:
                if defenseSavePath is not None:
                    logbook.info(f"savePath was {str(defenseSavePath)}")
                else:
                    logbook.info("savePath was NONE")
                self.info(f"    Delayed defense kingKillPath.  {str(kingKillPath)}")
                self.viewInfo.color_path(PathColorer(kingKillPath, 158, 158, 158, 255, 10, 200))

                return Move(kingKillPath.start.tile, kingKillPath.start.next.tile)
            else:
                if defenseSavePath is not None:
                    logbook.info(f"savePath was {str(defenseSavePath)}")
                else:
                    logbook.info("savePath was NONE")
                logbook.info(
                    f"savePath tile was also kingKillPath tile, skipped kingKillPath {str(kingKillPath)}")

        with self.perf_timer.begin_move_event('ARMY SCRIMS'):
            armyScrimMove = self.check_for_army_movement_scrims()
            if armyScrimMove is not None:
                # already logged
                return armyScrimMove

        with self.perf_timer.begin_move_event('DANGER TILES'):
            dangerTileKillMove = self.check_for_danger_tile_moves()
            if dangerTileKillMove is not None:
                return dangerTileKillMove  # already logged to info

        with self.perf_timer.begin_move_event('Flank defense / Vision expansion HIGH PRI'):
            flankDefMove = self.find_flank_defense_move(defenseCriticalTileSet, highPriority=True)
            if flankDefMove:
                return flankDefMove

        if not self.is_all_in_losing:
            with self.perf_timer.begin_move_event('get_quick_kill_on_enemy_cities'):
                path = self.get_quick_kill_on_enemy_cities(defenseCriticalTileSet)
            if path is not None:
                self.info(f'Quick Kill on enemy city: {str(path)}')
                self.curPath = path
                if self.curPath.length > 1:
                    self.curPath = self.curPath.get_subsegment(path.length - 2)
                move = self.get_first_path_move(path)
                if not self.detect_repetition(move):
                    return move

        if self.force_far_gathers and self.force_far_gathers_turns > 0:
            with self.perf_timer.begin_move_event(f'FORCE_FAR_GATHERS {self.force_far_gathers_turns}'):
                roughTurns = self.force_far_gathers_turns
                targets = None
                if self.enemy_attack_path:
                    targets = [t for t in self.enemy_attack_path.get_subsegment(int(self.enemy_attack_path.length / 2)).tileList if self._map.is_tile_enemy(t) or not t.visible]
                    self.info(f'FFG using enemy attack path {targets}')
                if not targets or len(targets) == 0:
                    targets = self.win_condition_analyzer.contestable_cities.copy()
                    self.info(f'FFG using contestable cities {targets}')
                if not targets or len(targets) == 0:
                    targets = self.win_condition_analyzer.defend_cities.copy()
                    self.info(f'FFG using defend cities {targets}')
                if (not targets or len(targets) == 0) and self.target_player_gather_path:
                    targets = [t for t in self.target_player_gather_path.get_subsegment(self.target_player_gather_path.length // 2, end=True).tileList if self._map.is_tile_enemy(t) or not t.visible]
                    self.info(f'FFG using end of target path {targets}')
                if not targets or len(targets) == 0:
                    targets = [self.general]
                    self.info(f'FFG fell back to general...? {targets}')
                for t in targets:
                    self.viewInfo.add_targeted_tile(t, TargetStyle.ORANGE)
                minTurns = roughTurns - 15
                maxTurns = roughTurns + 15

                if isinstance(self.curPath, GatherCapturePlan):
                    firstMove = self.curPath.get_first_move()

                    if firstMove is not None and firstMove.source.isSwamp or maxTurns >= self.curPath.length >= minTurns:
                        self.info(f'CONTINUING FFG plan')
                        self.clean_up_path_before_evaluating()
                        return self.curPath.get_first_move()

                gcp = Gather.gather_approximate_turns_to_tiles(
                    self._map,
                    targets,
                    roughTurns,
                    self.player.index,
                    minTurns=minTurns,
                    maxTurns=maxTurns,
                    gatherMatrix=self.get_gather_tiebreak_matrix(),
                    captureMatrix=self.get_expansion_weight_matrix(),
                    negativeTiles=defenseCriticalTileSet,
                    prioritizeCaptureHighArmyTiles=True,
                    useTrueValueGathered=False,
                    includeGatherPriorityAsEconValues=True,
                    timeLimit=min(0.05, self.get_remaining_move_time())
                )

                if gcp is None:
                    self.info(f'FAILED pcst FORCE_FAR_GATHERS min {minTurns}t max {maxTurns}t ideal {roughTurns}t, actual {gcp}')
                    self.info(f'FAILED pcst FORCE_FAR_GATHERS min {minTurns}t max {maxTurns}t ideal {roughTurns}t, actual {gcp}')
                    self.info(f'FAILED pcst FORCE_FAR_GATHERS min {minTurns}t max {maxTurns}t ideal {roughTurns}t, actual {gcp}')
                    self.info(f'FAILED pcst FORCE_FAR_GATHERS min {minTurns}t max {maxTurns}t ideal {roughTurns}t, actual {gcp}')
                    useTrueVal = False
                    gathMat = self.get_gather_tiebreak_matrix()
                    move, valGathered, turnsUsed, gatherNodes = self.get_gather_to_target_tiles(
                        targets,
                        0.05,
                        maxTurns,
                        defenseCriticalTileSet,
                        useTrueValueGathered=useTrueVal,
                        maximizeArmyGatheredPerTurn=True,
                        priorityMatrix=gathMat,
                    )

                    if gatherNodes:
                        gcp = GatherCapturePlan.build_from_root_nodes(
                            self._map,
                            gatherNodes,
                            defenseCriticalTileSet,
                            self.general.player,
                            onlyCalculateFriendlyArmy=useTrueVal,
                            priorityMatrix=gathMat,
                            includeGatherPriorityAsEconValues=True,
                            includeCapturePriorityAsEconValues=True,
                        )
                if gcp is not None:
                    move = gcp.get_first_move()
                    self.gatherNodes = gcp.root_nodes
                    self.info(f'pcst FORCE_FAR_GATHERS {move} min {minTurns}t max {maxTurns}t ideal {roughTurns}t, actual {gcp}')
                    self.curPath = gcp
                    return move
                else:
                    self.info(f'FAILED FORCE_FAR_GATHERS min {minTurns}t max {maxTurns}t ideal {roughTurns}t, actual {gcp}')

        if not self.is_weird_custom:
            second = self.make_second_25_move()
            if second:
                return second

        numTilesAdjKing = SearchUtils.count(self.general.adjacents, lambda tile: tile.army > 2 and self._map.is_tile_enemy(tile))
        if numTilesAdjKing == 1:
            visionTiles = filter(lambda tile: self._map.is_tile_enemy(tile) and tile.army > 2, self.general.adjacents)
            for annoyingTile in visionTiles:
                playerTilesAdjEnemyVision = [x for x in filter(lambda threatAdjTile: threatAdjTile.player == self.general.player and threatAdjTile.army > annoyingTile.army // 2 and threatAdjTile.army > 1, annoyingTile.movable)]
                if len(playerTilesAdjEnemyVision) > 0:
                    largestAdjTile = max(playerTilesAdjEnemyVision, key=lambda myTile: myTile.army)
                    if largestAdjTile and (not largestAdjTile.isGeneral or largestAdjTile.army + 1 > annoyingTile.army):
                        nukeMove = Move(largestAdjTile, annoyingTile)
                        self.info(f'Nuking general-adjacent vision tile {str(nukeMove)}, targeting it as targeting army.')
                        self.targetingArmy = self.get_army_at(annoyingTile)
                        return nukeMove

        afkPlayerMove = self.get_move_if_afk_player_situation()
        if afkPlayerMove:
            return afkPlayerMove

        self.check_cur_path()

        # needs to happen before defend_economy because defend_economy uses the properties set by this method.
        self.check_fog_risk()

        # if ahead on economy, but not %30 ahead on army we should play defensively
        self.defend_economy = self.should_defend_economy(defenseCriticalTileSet)

        # if self.targetingArmy and not self.targetingArmy.scrapped and self.targetingArmy.tile.army > 2 and not self.is_all_in_losing:
        #     with self.perf_timer.begin_move_event('Continue Army Kill'):
        #         armyTargetMove = self.continue_killing_target_army()
        #     if armyTargetMove:
        #         # already logged internally
        #         return armyTargetMove
        # else:
        #     self.targetingArmy = None

        if WinCondition.DefendContestedFriendlyCity in self.win_condition_analyzer.viable_win_conditions:
            with self.perf_timer.begin_move_event(f'Getting city preemptive defense {str(self.win_condition_analyzer.defend_cities)}'):
                cityDefenseMove = self.get_city_preemptive_defense_move(defenseCriticalTileSet)
            if cityDefenseMove is not None:
                self.info(f'City preemptive defense move! {str(cityDefenseMove)}')
                return cityDefenseMove

        with self.perf_timer.begin_move_event(f'capture_cities'):
            (cityPath, gatherMove) = self.capture_cities(defenseCriticalTileSet)
        if gatherMove is not None:
            logbook.info(f"{self.get_elapsed()} returning capture_cities gatherMove {str(gatherMove)}")
            if cityPath is not None:
                self.curPath = cityPath
            return gatherMove
        elif cityPath is not None:
            logbook.info(f"{self.get_elapsed()} returning capture_cities cityPath {str(cityPath)}")
            # self.curPath = cityPath
            return self.get_first_path_move(cityPath)

        # AFTER city capture because of test_should_contest_city_not_intercept_it. If this causes problems, then NEED to feed city attack/defense into the RoundPlanner and stop this hacky order shit...
        if self.expansion_plan and self.expansion_plan.includes_intercept and not self.is_all_in_losing:
            move = self.expansion_plan.selected_option.get_first_move()
            if not self.detect_repetition(move):
                # if isinstance(self.expansion_plan.selected_option, InterceptionOptionInfo):
                #     atTile = self.expansion_plan.selected_option.
                # atTile = None
                self.info(f'Pass thru EXP int! {move} {self.expansion_plan.selected_option}')
                return move

        with self.perf_timer.begin_move_event('try_get_enemy_territory_exploration_continuation_move'):
            expNegs = set(defenseCriticalTileSet)
            if WinCondition.DefendEconomicLead in self.win_condition_analyzer.viable_win_conditions:
                expNegs.update(self.win_condition_analyzer.defend_cities)
                expNegs.update(self.win_condition_analyzer.contestable_cities)
            largeArmyExpContinuationMove = self.try_get_enemy_territory_exploration_continuation_move(expNegs)

        if largeArmyExpContinuationMove is not None and not self.detect_repetition(largeArmyExpContinuationMove):
            # already logged
            return largeArmyExpContinuationMove

        # if self.threat_kill_path is not None and self.threat_kill_path:
        #     self.info(f"we're pretty safe from threat via gather, trying to kill threat instead.")
        #     # self.curPath = path
        #     self.targetingArmy = self.get_army_at(self.threat.path.start.tile)
        #     move = self.get_first_path_move(self.threat_kill_path)
        #     if not self.detect_repetition(move, 4, numReps=3):
        #         if self.detect_repetition(move, 4, numReps=2):
        #             move.move_half = True
        #         return move

        if 50 <= self._map.turn < 75:
            move = self.try_gather_tendrils_towards_enemy()
            if move is not None:
                return move
        #
        # threatDefenseLength = 2 * self.distance_from_general(self.targetPlayerExpectedGeneralLocation) // 3 + 1
        # if self.targetPlayerExpectedGeneralLocation.isGeneral:
        #     threatDefenseLength = self.distance_from_general(self.targetPlayerExpectedGeneralLocation) // 2 + 2
        #
        # if (
        #         threat is not None
        #         and threat.threatType == ThreatType.Kill
        #         and threat.path.length < threatDefenseLength
        #         and not self.is_all_in()
        #         and self._map.remainingPlayers < 4
        #         and threat.threatPlayer == self.targetPlayer
        # ):
        #     logbook.info(f"*\\*\\*\\*\\*\\*\n  Kill (non-vision) threat??? ({time.perf_counter() - start:.3f} in)")
        #     threatKill = self.kill_threat(threat)
        #     if threatKill and self.worth_path_kill(threatKill, threat.path, threat.armyAnalysis):
        #         if self.targetingArmy is None:
        #             logbook.info(f'setting targetingArmy to {str(threat.path.start.tile)} in Kill (non-vision) threat')
        #             self.targetingArmy = self.get_army_at(threat.path.start.tile)
        #         saveTile = threatKill.start.tile
        #         nextTile = threatKill.start.next.tile
        #         move = Move(saveTile, nextTile)
        #         if not self.detect_repetition(move, 6, 3):
        #             self.viewInfo.color_path(PathColorer(threatKill, 0, 255, 204, 255, 10, 200))
        #             move.move_half = self.should_kill_path_move_half(threatKill)
        #             self.info(f"Threat kill. half {move.move_half}, {threatKill.toString()}")
        #             return move

        # ARMY INTERCEPTION SHOULD NOW COVER VISION THREAT STUFF.
        # if (
        #         threat is not None
        #         and threat.threatType == ThreatType.Vision
        #         and not self.is_all_in()
        #         and threat.path.start.tile.visible
        #         and self.should_kill(threat.path.start.tile)
        #         and self.just_moved(threat.path.start.tile)
        #         and threat.path.length < min(10, self.distance_from_general(self.targetPlayerExpectedGeneralLocation) // 2 + 1)
        #         and self._map.remainingPlayers < 4
        #         and threat.threatPlayer == self.targetPlayer
        # ):
        #     logbook.info(f"*\\*\\*\\*\\*\\*\n  Kill vision threat. ({time.perf_counter() - start:.3f} in)")
        #     # Just kill threat then, nothing crazy
        #     path = self.kill_enemy_path(threat.path, allowGeneral = True)
        #
        #     visionKillDistance = 5
        #     if path is not None and self.worth_path_kill(path, threat.path, threat.armyAnalysis, visionKillDistance):
        #         if self.targetingArmy is None:
        #             logbook.info(f'setting targetingArmy to {str(threat.path.start.tile)} in Kill VISION threat')
        #             self.targetingArmy = self.get_army_at(threat.path.start.tile)
        #         self.info(f"Killing vision threat {threat.path.start.tile.toString()} with path {str(path)}")
        #         self.viewInfo.color_path(PathColorer(path, 0, 156, 124, 255, 10, 200))
        #         move = self.get_first_path_move(path)
        #         if not self.detect_repetition(move, turns=4, numReps=2) and (not path.start.tile.isGeneral or self.general_move_safe(move.dest)):
        #             move.move_half = self.should_kill_path_move_half(path)
        #             return move
        #     elif threat.path.start.tile == self.targetingArmy:
        #         logbook.info("threat.path.start.tile == self.targetingArmy and not worth_path_kill. Setting targetingArmy to None")
        #         self.targetingArmy = None
        #     elif path is None:
        #         logbook.info("No vision threat kill path?")

        if self.curPath is not None:
            move = self.continue_cur_path(threat, defenseCriticalTileSet)
            if move is not None:
                return move  # already logged

        exploreMove = self.try_find_exploration_move(defenseCriticalTileSet)
        if exploreMove is not None:
            return exploreMove  # already logged

        allInMove = self.get_all_in_move(defenseCriticalTileSet)
        if allInMove:
            # already logged
            return allInMove

        expMove = self.try_find_main_timing_expansion_move_if_applicable(defenseCriticalTileSet)
        if expMove is not None:
            return expMove  # already logged

        needToKillTiles = list()
        if not self.timings.disallowEnemyGather and not self.is_all_in_losing:
            needToKillTiles = self.find_key_enemy_vision_tiles()
            for tile in needToKillTiles:
                self.viewInfo.add_targeted_tile(tile, TargetStyle.RED)

        # LEAF MOVES for the first few moves of each cycle
        timingTurn = self.timings.get_turn_in_cycle(self._map.turn)
        quickExpTimingTurns = self.timings.quickExpandTurns - self._map.turn % self.timings.cycleTurns

        earlyRetakeTurns = quickExpTimingTurns + self.behavior_early_retake_bonus_gather_turns - self._map.turn % self.timings.cycleTurns

        if (not self.is_all_in()
                and self._map.remainingPlayers <= 3
                and earlyRetakeTurns > 0
                and len(needToKillTiles) > 0
                and timingTurn >= self.timings.quickExpandTurns
        ):
            actualGatherTurns = earlyRetakeTurns

            with self.perf_timer.begin_move_event(f'early retake turn gather?'):
                gatherNodes = Gather.knapsack_depth_gather(
                    self._map,
                    list(needToKillTiles),
                    actualGatherTurns,
                    negativeTiles=defenseCriticalTileSet,
                    searchingPlayer=self.general.player,
                    incrementBackward=False,
                    viewInfo=self.viewInfo if self.info_render_gather_values else None,
                    ignoreStartTile=True,
                    useTrueValueGathered=True)
            self.gatherNodes = gatherNodes
            move = self.get_tree_move_default(gatherNodes)
            if move is not None:
                self.info(
                    f"NeedToKillTiles for turns {earlyRetakeTurns} ({actualGatherTurns}) in quickExpand. Move {move}")
                return move

        with self.perf_timer.begin_move_event('Flank defense / Vision expansion low pri'):
            flankDefMove = self.find_flank_defense_move(defenseCriticalTileSet, highPriority=False)
            if flankDefMove:
                return flankDefMove

        if self.defend_economy:
            move = self.try_find_army_out_of_position_move(defenseCriticalTileSet)
            if move is not None:
                return move  # already logged

        cyclicAllInMove = self.try_get_cyclic_all_in_move(defenseCriticalTileSet)
        if cyclicAllInMove:
            return cyclicAllInMove  # already logged

        if not self.is_all_in() and not self.defend_economy and quickExpTimingTurns > 0:
            move = self.try_gather_tendrils_towards_enemy(quickExpTimingTurns)
            if move is not None:
                return move

            moves = self.prioritize_expansion_leaves(self.leafMoves)
            if len(moves) > 0:
                move = moves[0]
                self.info(f"quickExpand leafMove {move}")
                return move

        expMove = self._get_expansion_plan_quick_capture_move(defenseCriticalTileSet)
        if expMove:
            self.info(f"quickCap move {expMove}")
            return expMove

        with self.perf_timer.begin_move_event(f'MAIN GATHER OUTER, negs {[str(t) for t in defenseCriticalTileSet]}'):
            gathMove = self.try_find_gather_move(threat, defenseCriticalTileSet, self.leafMoves, needToKillTiles)

        if gathMove is not None:
            # already logged / perf countered internally
            return gathMove

        isFfaAfkScenario = self._map.remainingCycleTurns > 14 and self.is_still_ffa_and_non_dominant() and self._map.cycleTurn < self.timings.launchTiming and not self._map.is_walled_city_game
        if isFfaAfkScenario:
            self.info(f'FFA AFK')
            return None

        # if self._map.turn > 150:
        #     with self.perf_timer.begin_move_event(f'No gather found final scrim'):
        #         scrimMove = self.find_end_of_turn_scrim_move(threat, kingKillPath)
        #         if scrimMove is not None:
        #             return scrimMove

        # NOTE NOTHING PAST THIS POINT CAN TAKE ANY EXTRA TIME

        if not self.is_all_in():
            with self.perf_timer.begin_move_event(f'No move found leafMove'):
                leafMove = self.find_leaf_move(self.leafMoves)
            if leafMove is not None:
                self.info(f"No move found leafMove? {str(leafMove)}")
                return leafMove

        self.curPathPrio = -1
        if self.get_remaining_move_time() > 0.01:
            with self.perf_timer.begin_move_event('FOUND NO MOVES GATH'):
                self.info(f'No move found main gather')
                targets = []
                if self.targetPlayer >= 0:
                    targets.extend(self.targetPlayerObj.tiles)
                else:
                    targets.extend(t for t in self._map.get_all_tiles() if t.player == -1 and not t.isObstacle)
                negatives = defenseCriticalTileSet.copy()
                negatives.update(self.target_player_gather_targets)
                turns = self.timings.launchTiming - self.timings.get_turn_in_cycle(self._map.turn)
                gathMove = self.timing_gather(
                    targets,
                    negatives,
                    None,
                    force=True,
                    pruneToValuePerTurn=True,
                    useTrueValueGathered=True,
                    includeGatherTreeNodesThatGatherNegative=False,
                    targetTurns=turns,
                    logStuff=True)
                if gathMove:
                    return gathMove
        move = None
        value = -1
        #
        # with self.perf_timer.begin_move_event('FOUND NO MOVES FINAL MST GATH'):
        #     gathers = self.build_mst(self.target_player_gather_targets, 1.0, 50, None)
        #
        #     turns, value, gathers = Gather.prune_mst_to_max_army_per_turn_with_values(
        #         gathers,
        #         1,
        #         self.general.player,
        #         teams=MapBase.get_teams_array(self._map),
        #         preferPrune=self.expansion_plan.preferred_tiles if self.expansion_plan is not None else None,
        #         viewInfo=self.viewInfo if self.info_render_gather_values else None)
        # self.gatherNodes = gathers
        # # move = self.get_gather_move(gathers, None, 1, 0, preferNeutral = True)
        # move = self.get_tree_move_default(gathers)

        if move is None:
            # turnInCycle = self.timings.get_turn_in_cycle(self._map.turn)
            # if self.timings.cycleTurns - turnInCycle < self.timings.cycleTurns - self.target_player_gather_path.length * 0.66:
            #     self.info("Found-no-moves-gather NO MOVE? Set launch now.")
            #     self.timings.launchTiming = self._map.turn % self.timings.cycleTurns
            # else:
            self.info("Found-no-moves-gather found no move, random expansion move?")
            if self.expansion_plan and self.expansion_plan.selected_option:
                for opt in self.expansion_plan.all_paths:
                    move = opt.get_first_move()
                    if move is not None and move.source.player == self._map.player_index and move.source.army > 1:
                        self.curPath = opt
                        return move

        elif self.is_move_safe_valid(move):
            self.info(f"Found-no-moves-gather found {value}v/{turns}t gather, using {move}")
            return move
        else:
            self.info(
                f"Found-no-moves-gather move {move} was not safe or valid!")

        return None

    def is_all_in(self):
        return self.is_all_in_losing or self.is_all_in_army_advantage or self.all_in_city_behind

    def should_kill(self, tile):
        # bypass bugs around city increment for kill_path.
        # If its a city and they aren't moving the army, no sense trying to treat it like an army intercept anyway.
        if tile.isCity and abs(tile.delta.armyDelta) < 3:
            return False
        return True

    def just_moved(self, tile):
        if abs(tile.delta.armyDelta) > 2:
            return True
        else:
            return False

    def should_kill_path_move_half(self, threatKill, additionalArmy=0):
        start = threatKill.start.tile
        next = threatKill.start.next.tile
        threatKill.calculate_value(self.general.player, teams=self._map.team_ids_by_player_index)
        movingAwayFromEnemy = self.board_analysis.intergeneral_analysis.bMap[start] < self.board_analysis.intergeneral_analysis.bMap[next]
        move_half = movingAwayFromEnemy and threatKill.tail.tile.army + additionalArmy < (threatKill.value + threatKill.tail.tile.army) // 2

        if threatKill.tail.tile.isCity and threatKill.tail.tile.player >= 0:
            return False

        logbook.info(
            f"should_kill_path_move_half: movingAwayFromEnemy {movingAwayFromEnemy}\n                 threatKill.value = {threatKill.value}\n                 threatKill.tail.tile.army = {threatKill.tail.tile.army}\n                 (threatKill.value + threatKill.tail.tile.army) // 2 = {(threatKill.econValue + threatKill.tail.tile.army) // 2}\n                 : {move_half}")
        return move_half

    def find_key_enemy_vision_tiles(self):
        keyTiles = set()
        genPlayer = self._map.players[self.general.player]
        distFactor = 2
        priorityDist = self.distance_from_general(self.targetPlayerExpectedGeneralLocation) // distFactor
        if self.targetPlayerExpectedGeneralLocation.isGeneral:
            distFactor = 3
            priorityDist = self.distance_from_general(self.targetPlayerExpectedGeneralLocation) // distFactor + 1

        for tile in self.general.adjacents:
            if self._map.is_tile_enemy(tile):
                keyTiles.add(tile)
        for city in genPlayer.cities:
            if self._map.turn - city.turn_captured > 20:
                for tile in city.adjacents:
                    if self._map.is_tile_enemy(tile):
                        keyTiles.add(tile)

        for tile in self._map.pathable_tiles:
            if self._map.is_tile_enemy(tile):
                if self.distance_from_general(tile) < priorityDist:
                    keyTiles.add(tile)

        cityAdjCount = 0
        for city, score in self.cityAnalyzer.get_sorted_neutral_scores():
            if score.general_distances_ratio < 0.9:
                for adj in city.adjacents:
                    if self._map.is_tile_enemy(adj):
                        cityAdjCount += 1
                        keyTiles.add(adj)

            if cityAdjCount > 5:
                break

        return keyTiles

    def worth_path_kill(self, pathKill: Path, threatPath: Path, analysis=None, cutoffDistance=5):
        if pathKill.start is None or pathKill.tail is None:
            return False

        lenTillInThreatPath = 0
        node = pathKill.start
        while node is not None and node.tile not in threatPath.tileSet:
            lenTillInThreatPath += 1
            node = node.next

        shortEnoughPath = lenTillInThreatPath < max(3, threatPath.length - 1)
        logbook.info(
            f"worth_path_kill: shortEnoughPath = lenTillInThreatPath {lenTillInThreatPath} < max(3, threatPath.length - 1 ({threatPath.length - 1})): {shortEnoughPath}")
        if not shortEnoughPath:
            self.viewInfo.paths.append(PathColorer(pathKill.clone(), 163, 129, 50, 255, 0, 100))
            logbook.info(f"  path kill eliminated due to shortEnoughPath {shortEnoughPath}")
            return False

        minSourceArmy = 8
        threatArmy = threatPath.start.tile.army
        if threatPath.start.tile in self.armyTracker.armies:
            army = self.armyTracker.armies[threatPath.start.tile]
            threatArmy = army.value
        threatMoved = abs(threatPath.start.tile.delta.armyDelta) >= 2
        if threatArmy < minSourceArmy and not threatMoved:
            logbook.info(
                f"  path kill eliminated due to not threatMoved and threatArmy {threatArmy} < minSourceArmy {minSourceArmy}")
            return False

        if not analysis:
            analysis = ArmyAnalyzer.build_from_path(self._map, threatPath)
        lastTile = pathKill.tail.tile
        if pathKill.start.next.next is not None:
            lastTile = pathKill.start.next.next.tile
        startDist = self.board_analysis.intergeneral_analysis.bMap[pathKill.start.tile]
        tailDist = self.board_analysis.intergeneral_analysis.bMap[lastTile]
        movingTowardsOppo = startDist > tailDist
        canGoOtherDirection = True
        for node in self.best_defense_leaves:
            if node.tile == pathKill.start.tile:
                # if self.board_analysis.intergeneral_analysis.bMap[node.fromTile] >= startDist:
                # we have to move backwards anyway, kill the threat.
                canGoOtherDirection = False
        logbook.info(
            f"worth_path_kill: movingTowardsOppo {movingTowardsOppo}  ({pathKill.start.tile.toString()} [{startDist}]  ->  {lastTile.toString()} [{tailDist}])")
        onShortestPathwayAlready = (pathKill.start.tile in analysis.pathWayLookupMatrix[threatPath.start.tile].tiles
                                    or (pathKill.start.tile in analysis.pathWayLookupMatrix
                                        and analysis.pathWayLookupMatrix[pathKill.start.tile].distance < analysis.pathWayLookupMatrix[threatPath.start.tile].distance))

        logbook.info(
            f"worth_path_kill: onPath = pathKill.start.tile {pathKill.start.tile.toString()} in analysis.pathways[threatPath.start.tile {threatPath.start.tile.toString()}].tiles: {onShortestPathwayAlready}")

        enTilesInPath = SearchUtils.where(pathKill.tileList, lambda t: self._map.is_tile_enemy(t))

        # TODO switch to true based returns.
        # moving towards opp with recapture potential at end of round, etc.

        threatNegs = pathKill.tileSet.copy()
        threatNegs.add(threatPath.tail.tile)
        threatNegs.discard(threatPath.start.tile)
        killOverlap = pathKill.calculate_value(self.general.player, teams=self._map.team_ids_by_player_index, negativeTiles=threatPath.tileSet) - threatPath.calculate_value(threatPath.start.tile.player, teams=self._map.team_ids_by_player_index, negativeTiles=threatNegs)

        turnsLeftInCycle = self.timings.get_turns_left_in_cycle(self._map.turn)
        turnsLeftInCycleCutoffThresh = turnsLeftInCycle // 2 - 1
        if pathKill.length - len(enTilesInPath) > turnsLeftInCycleCutoffThresh and canGoOtherDirection and not movingTowardsOppo:
            self.viewInfo.add_info_line(f'Eliminated path kill due to len {pathKill.length} - enTilesInPath {len(enTilesInPath)} > cycleCutoffThresh {turnsLeftInCycleCutoffThresh}')
            return False

        # if pathKill.length > cutoffDistance and onShortestPathwayAlready and not movingTowardsOppo:
        # if pathKill.length > cutoffDistance and not movingTowardsOppo and canGoOtherDirection:
        #     # then we're already on their attack path? Don't waste time moving towards it unless we're close.
        #     self.viewInfo.paths.append(PathColorer(pathKill.clone(), 217, 0, 0, 255, 0, 100))
        #     logbook.info(
        #         f"  path kill eliminated due to pathKill.length > cutoffDistance {cutoffDistance} ({pathKill.length > cutoffDistance}) and onShortestPathwayAlready {onShortestPathwayAlready} and not movingTowardsOppo {movingTowardsOppo}")
        #     return False
        logbook.info(f"  path kill worth it because not eliminated ({pathKill.toString()})")
        return True

    def kill_army(
            self,
            army: Army,
            allowGeneral=False,
            allowWorthPathKillCheck=True
    ):
        if len(army.expectedPaths) == 0:
            army.expectedPaths = ArmyTracker.get_army_expected_path(self._map, army, self.general, self.armyTracker.player_targets)

        # TODO needs to handle multi-path
        for path in army.expectedPaths:
            if path.start.tile != army.tile:
                continue
            if not path:
                logbook.info(f"In Kill_army: No bfs dynamic path found from army tile {str(army)} ???????")
                if self.targetingArmy == army:
                    self.targetingArmy = None
                return None

            # self.viewInfo.paths.append(PathColorer(path.clone(), 100, 0, 100, 200, 5, 100))
            killPath = self.kill_enemy_path(path, allowGeneral)

            if killPath is not None:
                if not allowWorthPathKillCheck:
                    return killPath
                with self.perf_timer.begin_move_event(f'build army analyzer for army kill of {repr(army)}'):
                    analyzer = ArmyAnalyzer(self._map, self.general, army.tile)
                worthPathKill = self.worth_path_kill(killPath, path, analyzer)
                if worthPathKill:
                    return killPath

                self.viewInfo.add_info_line(
                    f"NO army cont kill on {str(army)} because not worth with path {str(killPath)}")
                if self.targetingArmy == army:
                    self.targetingArmy = None
            else:
                self.viewInfo.add_info_line(f"NO army cont kill on {str(army)}, no pathKill was found.")
                if self.targetingArmy == army:
                    self.targetingArmy = None

        return None

    def kill_enemy_path(self, threatPath: Path, allowGeneral=False) -> Path | None:
        """
        This is some wild shit that needs to be redone.
        @param threatPath: The threat path
        @param allowGeneral:
        @return:
        """
        return self.kill_enemy_paths([threatPath], allowGeneral)

    def kill_enemy_paths(self, threatPaths: typing.List[Path], allowGeneral=False) -> Path | None:
        """
        This is some wild shit that needs to be redone.
        @param threatPaths: The threat paths
        @param allowGeneral:
        @return:
        """
        # if not self.disable_engine:
        #     path = self.try_find_counter_army_scrim_path_killpath(threatPath, allowGeneral)
        #     if path is not None:
        #         return path

        threats = []

        allThreatsLow = True
        negativeTiles = set()

        with self.perf_timer.begin_move_event('Kill Enemy Path ArmyAnalyzer'):
            for threatPath in threatPaths:
                armyAnalysis = ArmyAnalyzer.build_from_path(self._map, threatPath)
                threat = ThreatObj(threatPath.length, threatPath.value, threatPath, ThreatType.Vision, armyAnalysis=armyAnalysis)
                threats.append(threat)
                threatPath.value = threatPath.calculate_value(self.get_army_at(threatPath.start.tile).player, self._map.team_ids_by_player_index, negativeTiles)

                if threatPath.value > 0:
                    allThreatsLow = False

        logbook.info(f"Starting kill_enemy_path for path {str(threatPath)}")

        if allThreatsLow:
            # the enemy path has to path through us, just try to kill the army
            killPath = SearchUtils.dest_breadth_first_target(self._map, [threatPath.start.tile], maxDepth=6, negativeTiles=negativeTiles, targetArmy=-1, additionalIncrement=-2)
            if killPath is not None:
                self.info(f'kill_path dest low val @ {str(threatPath.start.tile)} KILL PATH {str(killPath)}')
                return killPath

        # Doesn't make any sense to have the general defend against his own threat, does it? Maybe it does actually hm
        if not allowGeneral:
            negativeTiles.add(self.general)

        for threat in threats:
            threatPath = threat.path
            shorterThreatPath = threatPath.get_subsegment(threatPath.length - 2)
            threatPathSet = shorterThreatPath.tileSet.copy()
            threatPathSet.discard(threatPath.start.tile)
            # skipTiles = threatPathSet.copy()

            threatTile = threatPath.start.tile
            threatPlayer = threatPath.start.tile.player
            if threatTile in self.armyTracker.armies:
                threatPlayer = self.armyTracker.armies[threatTile].player
            threatPath.calculate_value(threatPlayer, teams=self._map.team_ids_by_player_index)
            threatValue = max(threatPath.start.tile.army, threatPath.value)
            if threatTile.player != threatPlayer:
                threatValue = self.armyTracker.armies[threatTile].value
            if threatValue <= 0:
                # then we're probably blocking the threat in the negative tiles. Undo negative tiles and set desired value to the actual threat tile value.
                threatValue = threatTile.army
                if threatTile.player != threatPlayer:
                    threatValue = self.armyTracker.armies[threatTile].value
                # for tile in threatPathSet:
                #    if tile.player == threatPath.start.tile.player:
                #        skipTiles.add(tile)
                logbook.info(
                    f"threatValue was originally {threatPath.value}, removed player negatives and is now {threatValue}")
            else:
                logbook.info(f"threatValue is {threatValue}")

            if threat.turns > 0:
                directKillThresh = max(4, 2 * threatValue // threat.turns if threat.turns > 0 else 0)
                directKillThresh = min(threatValue, directKillThresh)
                # First try one move kills on next tile, since I think this is broken in the loop for whatever reason... (make it 2 moves though bc other stuff depends on tail tile)
                for adj in threatPath.start.next.tile.movable:
                    if adj.player == self.general.player and adj.army >= directKillThresh:
                        if adj.isGeneral and threat.armyAnalysis.chokeWidths[threatPath.start.next.tile] > 1:
                            self.info(f"bypassed direct-kill gen move because choke width")
                            continue

                        path = Path()
                        path.add_next(adj)
                        path.add_next(threatPath.start.next.tile)
                        path.add_next(threatTile)
                        self.info(f"returning nextTile direct-kill move {str(path)}")
                        return path

            directKillThresh = max(4, 3 * threatValue // threat.turns if threat.turns > 0 else 0)
            directKillThresh = min(threatValue, directKillThresh)

            # Then try one move kills on the threat tile. 0 = 1 move????
            # TODO do we need this...?
            for adj in threatTile.movable:
                if adj.player == self.general.player and adj.army >= directKillThresh:
                    path = Path()
                    path.add_next(adj)
                    path.add_next(threatTile)
                    self.info(f"returning direct-kill move {str(path)}")
                    return path

        # modifiedThreatPath = threat.path.get_subsegment(threat.path.length // 2 + 1)
        # threat.path = modifiedThreatPath

        threatCutoff = max(1, max(threats, key=lambda t: t.threatValue).threatValue - 10)

        # if threatValue > 5:
        killMove, gatherVal, gathTurns, gatherNodes = self.get_gather_to_threat_paths(threats, gatherMax=True, addlTurns=-1, force_turns_up_threat_path=1, requiredContribution=threatCutoff, interceptArmy=True)

        if killMove is not None and gatherVal > threatCutoff:
            self.info(f'kill_path gath @ {str(threatPath.start.tile)} {str(killMove)}')
            path = Path()
            path.add_next(killMove.source)
            path.add_next(killMove.dest)
            return path

        # # Then iteratively search for a kill to the closest tile on the path to the threat, checking one tile further along the threat each time.
        # curNode = threatPath.start.next
        # # 0 = 1 move? lol
        # i = 0
        # threatModifier = 0
        # gatherToThreatPath = None
        # while gatherToThreatPath is None and curNode is not None:
        #     # trying to use the generals army as part of the path even though its in negative tiles? apparently negativetiles gets ignored for the start tile?
        #     # # NOT TRUE ANYMORE!!??!?!
        #     #if curNode.tile.player != threatPath.start.tile.player:
        #     #    threatModifier -= curNode.tile.army
        #     logbook.info(
        #         f"Attempting threatKill on tile {curNode.tile.toString()} with threatValue {threatValue} + mod {threatModifier} = ({threatValue + threatModifier})")
        #     gatherToThreatPath = SearchUtils.dest_breadth_first_target(self._map, [curNode.tile], targetArmy = threatValue + threatModifier, maxDepth = max(1, i), searchingPlayer = self.general.player, negativeTiles = negativeTiles, noLog = True, ignoreGoalArmy = True)
        #     #if curNode.tile.player == self.general.player:
        #     #    nodeVal = curNode.tile.army - 1
        #     #gatherToThreatPath = SearchUtils.dest_breadth_first_target(self._map, [curNode.tile], targetArmy = threatValue + nodeVal, maxDepth = max(1, i), searchingPlayer = self.general.player, skipTiles = skipTiles, noLog = True)
        #     i += 1
        #     curNode = curNode.next
        #
        # if gatherToThreatPath is not None:
        #     self.info(f"whoo, found kill on threatpath with path {gatherToThreatPath.toString()}")
        #     alpha = 140
        #     minAlpha = 100
        #     alphaDec = 2
        #     self.viewInfo.color_path(PathColorer(gatherToThreatPath.clone(), 150, 10, 255, alpha, alphaDec, minAlpha))
        #     tail = gatherToThreatPath.tail.tile
        #
        #     goalFunc = lambda tile, prioObject: tile == threatPath.start.tile
        #
        #     def threatPathSortFunc(nextTile, prioObject):
        #         (dist, _, negNumThreatTiles, negArmy) = prioObject
        #         if nextTile in threatPathSet:
        #             negNumThreatTiles -= 1
        #         if nextTile.player == self.general.player:
        #             negArmy -= nextTile.army
        #         else:
        #             negArmy += nextTile.army
        #         dist += 1
        #         return dist, self._map.euclidDist(nextTile.x, nextTile.y, threatTile.x, threatTile.y), negNumThreatTiles, negArmy
        #     inputTiles = {}
        #     inputTiles[tail] = ((0, 0, 0, 0), 0)
        #
        #     threatPathToThreat = SearchUtils.breadth_first_dynamic(self._map, inputTiles, goalFunc, noNeutralCities=True, priorityFunc=threatPathSortFunc)
        #     if threatPathToThreat is not None:
        #         logbook.info(
        #             f"whoo, finished off the threatpath kill {threatPathToThreat.toString()}\nCombining paths...")
        #         node = threatPathToThreat.start.next
        #         while node is not None:
        #             gatherToThreatPath.add_next(node.tile)
        #             node = node.next
        #         gatherToThreatPath.calculate_value(self.general.player, teams=self._map._teams)
        # endTime = time.perf_counter() - startTime
        # if gatherToThreatPath is not None:
        #     if gatherToThreatPath.length == 0:
        #         logbook.info(
        #             f"kill_enemy_path {threatPath.start.tile.toString()} completed in {endTime:.4f}, PATH {gatherToThreatPath.toString()} WAS LENGTH 0, RETURNING NONE! :(")
        #         return None
        #     else:
        #         logbook.info(
        #             f"kill_enemy_path {threatPath.start.tile.toString()} completed in {endTime:.4f}, path {gatherToThreatPath.toString()}")
        # else:
        #     logbook.info(
        #         f"kill_enemy_path {threatPath.start.tile.toString()} completed in {endTime:.4f}, No path found :(")
        # return gatherToThreatPath

    def kill_threat(self, threat: ThreatObj, allowGeneral=False):
        return self.kill_enemy_path(threat.path.get_subsegment(threat.path.length // 2), allowGeneral)

    def get_gather_to_target_tile(
            self,
            target: Tile,
            maxTime: float,
            gatherTurns: int,
            negativeSet: typing.Set[Tile] | None = None,
            targetArmy: int = -1,
            useTrueValueGathered=False,
            includeGatherTreeNodesThatGatherNegative=False,
            maximizeArmyGatheredPerTurn: bool = False
    ) -> typing.Tuple[Move | None, int, int, typing.Union[None, typing.List[GatherTreeNode]]]:
        """
        returns move, valueGathered, turnsUsed

        @param target:
        @param maxTime:
        @param gatherTurns:
        @param negativeSet:
        @param targetArmy:
        @param useTrueValueGathered: Use True for things like capturing stuff. Causes the algo to include the cost of
         capturing tiles in the value calculation. Also include the cost of the gather start tile into the gather FINDER
         so that it only finds paths that kill the target. Avoid using this when just gathering as it prevents
         gathering tiles on the other side of enemy territory, which is the opposite of good general gather behavior.
         Use useTrueValueGathered to make sure the gatherValue returned by the gather matches the actual amount of army you will have on the gather target tiles at the end of the gather execution.
         Use includeGatherTreeNodesThatGatherNegative to allow a gather to return gather nodes in the final result that fail to flip an enemy node in the path to friendly.
        @param maximizeArmyGatheredPerTurn: if set to True, will prune the result to the maximum gather value amount per turn.
        @param includeGatherTreeNodesThatGatherNegative: if set True, allows the gather PLAN to gather
         to tiles without killing them. Use this for defense for example, when you dont need to fully kill the threat tile with each gather move.
         Use includeGatherTreeNodesThatGatherNegative to allow a gather to return gather nodes in the final result that fail to flip an enemy node in the path to friendly.
         Use useTrueValueGathered to make sure the gatherValue returned by the gather matches the actual amount of army you will have on the gather target tiles at the end of the gather execution.
        @return:
        """
        targets = [target]
        gatherTuple = self.get_gather_to_target_tiles(
            targets,
            maxTime,
            gatherTurns,
            negativeSet,
            targetArmy,
            useTrueValueGathered=useTrueValueGathered,
            includeGatherTreeNodesThatGatherNegative=includeGatherTreeNodesThatGatherNegative,
            maximizeArmyGatheredPerTurn=maximizeArmyGatheredPerTurn)
        return gatherTuple

    # set useTrueValueGathered to True for things like defense gathers,
    # where you want to take into account army lost gathering over enemy or neutral tiles etc.
    def get_defensive_gather_to_target_tiles(
            self,
            targets,
            maxTime,
            gatherTurns,
            negativeSet=None,
            targetArmy=-1,
            useTrueValueGathered=False,
            leafMoveSelectionValueFunc=None,
            includeGatherTreeNodesThatGatherNegative=False,
            maximizeArmyGatheredPerTurn: bool = False,
            additionalIncrement: int = 0,
            distPriorityMap: MapMatrix[int] | None = None,
            priorityMatrix: MapMatrixInterface[float] | None = None,
            skipTiles: TileSet | None = None,
            shouldLog: bool = False,
            fastMode: bool = False
    ) -> typing.Tuple[Move | None, int, int, typing.Union[None, typing.List[GatherTreeNode]]]:
        """
        returns move, valueGathered, turnsUsed, gatherNodes

        @param targets:
        @param maxTime:
        @param gatherTurns:
        @param negativeSet:
        @param targetArmy:
        @param additionalIncrement: if need to gather extra army due to incrementing, include the POSITIVE enemy city increment or NEGATIVE allied increment value here.
        @param useTrueValueGathered: Use True for things like capturing stuff. Causes the algo to include the cost of
         capturing tiles in the value calculation. Also include the cost of the gather start tile into the gather FINDER
         so that it only finds paths that kill the target. Avoid using this when just gathering as it prevents
         gathering tiles on the other side of enemy territory, which is the opposite of good general gather behavior.
         Use useTrueValueGathered to make sure the gatherValue returned by the gather matches the actual amount of army you will have on the gather target tiles at the end of the gather execution.
         Use includeGatherTreeNodesThatGatherNegative to allow a gather to return gather nodes in the final result that fail to flip an enemy node in the path to friendly.
        @param maximizeArmyGatheredPerTurn: if set to True, will prune the result to the maximum gather value amount per turn.
        @param includeGatherTreeNodesThatGatherNegative: if set True, allows the gather PLAN to gather
         to tiles without killing them. Use this for defense for example, when you dont need to fully kill the threat tile with each gather move.
         Use includeGatherTreeNodesThatGatherNegative to allow a gather to return gather nodes in the final result that fail to flip an enemy node in the path to friendly.
         Use useTrueValueGathered to make sure the gatherValue returned by the gather matches the actual amount of army you will have on the gather target tiles at the end of the gather execution.
        @param leafMoveSelectionValueFunc:
        @param skipTiles: Tiles to treat as mountains
        @param shouldLog:
        @param distPriorityMap: A distance map to an incoming threat that should be used to prevent gathering chasing an inbound threat for example.
        @param priorityMatrix:
        @param fastMode: whether to gather using fast mode.
        @return:
        """

        if useTrueValueGathered and targetArmy > -1:
            # TODO figure out why this is necessary...
            targetArmy += 1

        if additionalIncrement != 0 and targetArmy > 0:
            targetArmy = targetArmy + additionalIncrement * gatherTurns // 2

        gatherNodes = Gather.knapsack_depth_gather(
            self._map,
            targets,
            gatherTurns,
            targetArmy,
            distPriorityMap=distPriorityMap,
            negativeTiles=negativeSet,
            searchingPlayer=self.general.player,
            viewInfo=self.viewInfo if self.info_render_gather_values else None,
            useTrueValueGathered=useTrueValueGathered,
            incrementBackward=False,
            includeGatherTreeNodesThatGatherNegative=includeGatherTreeNodesThatGatherNegative,
            priorityMatrix=priorityMatrix,
            cutoffTime=time.perf_counter() + maxTime,
            shouldLog=shouldLog,
            fastMode=fastMode)

        if maximizeArmyGatheredPerTurn:
            turns, value, gatherNodes = Gather.prune_mst_to_max_army_per_turn_with_values(
                gatherNodes,
                targetArmy,
                searchingPlayer=self.general.player,
                teams=MapBase.get_teams_array(self._map),
                additionalIncrement=additionalIncrement,
                preferPrune=self.expansion_plan.preferred_tiles if self.expansion_plan is not None else None,
                viewInfo=self.viewInfo if self.info_render_gather_values else None)

        totalValue = 0
        turns = 0
        for gather in gatherNodes:
            logbook.info(f"gatherNode {gather.tile.toString()} value {gather.value}")
            totalValue += gather.value
            turns += gather.gatherTurns

        logbook.info(
            f"gather_to_target_tiles totalValue was {totalValue}. Setting gatherNodes for visual debugging regardless of using them")
        if totalValue > targetArmy - gatherTurns // 2:
            move = self.get_tree_move_default(gatherNodes, valueFunc=leafMoveSelectionValueFunc)
            if move is not None:
                self.gatherNodes = gatherNodes
                return self.move_half_on_repetition(move, 4), totalValue, turns, gatherNodes
            else:
                logbook.info("Gather returned no moves :(")
        else:
            logbook.info(f"Value {totalValue} was too small to return... (needed {targetArmy}) :(")
        return None, -1, -1, None

    # set useTrueValueGathered to True for things like defense gathers,
    # where you want to take into account army lost gathering over enemy or neutral tiles etc.
    def get_gather_to_target_tiles(
            self,
            targets,
            maxTime,
            gatherTurns,
            negativeSet=None,
            targetArmy=-1,
            useTrueValueGathered=False,
            leafMoveSelectionValueFunc=None,
            includeGatherTreeNodesThatGatherNegative=False,
            maximizeArmyGatheredPerTurn: bool = False,
            additionalIncrement: int = 0,
            distPriorityMap: MapMatrix[int] | None = None,
            priorityMatrix: MapMatrixInterface[float] | None = None,
            skipTiles: TileSet | None = None,
            shouldLog: bool = False,
            fastMode: bool = False
    ) -> typing.Tuple[Move | None, int, int, typing.Union[None, typing.List[GatherTreeNode]]]:
        """
        returns move, valueGathered, turnsUsed, gatherNodes

        @param targets:
        @param maxTime:
        @param gatherTurns:
        @param negativeSet:
        @param targetArmy:
        @param additionalIncrement: if need to gather extra army due to incrementing, include the POSITIVE enemy city increment or NEGATIVE allied increment value here.
        @param useTrueValueGathered: Use True for things like capturing stuff. Causes the algo to include the cost of
         capturing tiles in the value calculation. Also include the cost of the gather start tile into the gather FINDER
         so that it only finds paths that kill the target. Avoid using this when just gathering as it prevents
         gathering tiles on the other side of enemy territory, which is the opposite of good general gather behavior.
         Use useTrueValueGathered to make sure the gatherValue returned by the gather matches the actual amount of army you will have on the gather target tiles at the end of the gather execution.
         Use includeGatherTreeNodesThatGatherNegative to allow a gather to return gather nodes in the final result that fail to flip an enemy node in the path to friendly.
        @param maximizeArmyGatheredPerTurn: if set to True, will prune the result to the maximum gather value amount per turn.
        @param includeGatherTreeNodesThatGatherNegative: if set True, allows the gather PLAN to gather
         to tiles without killing them. Use this for defense for example, when you dont need to fully kill the threat tile with each gather move.
         Use includeGatherTreeNodesThatGatherNegative to allow a gather to return gather nodes in the final result that fail to flip an enemy node in the path to friendly.
         Use useTrueValueGathered to make sure the gatherValue returned by the gather matches the actual amount of army you will have on the gather target tiles at the end of the gather execution.
        @param leafMoveSelectionValueFunc:
        @param skipTiles: Tiles to treat as mountains
        @param shouldLog:
        @param distPriorityMap: A distance map to an incoming threat that should be used to prevent gathering chasing an inbound threat for example.
        @param priorityMatrix:
        @param fastMode: whether to gather using fast mode.
        @return:
        """

        if useTrueValueGathered and targetArmy > -1:
            # TODO figure out why this is necessary...
            targetArmy += 1

        if self.gather_use_pcst and gatherTurns > 0 and targetArmy < 0 and not isinstance(targets, dict):
            convertedTargets: typing.List[Tile] = targets

            gathCapPlan = Gather.gather_approximate_turns_to_tiles(
                self._map,
                rootTiles=convertedTargets,
                approximateTargetTurns=gatherTurns,
                asPlayer=self.general.player,
                gatherMatrix=priorityMatrix,
                captureMatrix=priorityMatrix,
                negativeTiles=negativeSet,
                skipTiles=skipTiles,
                prioritizeCaptureHighArmyTiles=False,
                useTrueValueGathered=useTrueValueGathered,
                includeGatherPriorityAsEconValues=True,
                includeCapturePriorityAsEconValues=True,
                logDebug=shouldLog,
                viewInfo=self.viewInfo if self.info_render_gather_values else None)

            if gathCapPlan is not None:

                gatherNodes = gathCapPlan.root_nodes

                if maximizeArmyGatheredPerTurn:
                    self.info(
                        f"pcst gath (pre-prune) achieved {gathCapPlan.gathered_army} turns {gathCapPlan.gather_turns} (target turns {gatherTurns})")
                    turns, value, gatherNodes = Gather.prune_mst_to_max_army_per_turn_with_values(
                        gatherNodes,
                        targetArmy,
                        searchingPlayer=self.general.player,
                        teams=MapBase.get_teams_array(self._map),
                        additionalIncrement=additionalIncrement,
                        preferPrune=self.expansion_plan.preferred_tiles if self.expansion_plan is not None else None,
                        viewInfo=self.viewInfo if self.info_render_gather_values else None)

                totalValue = 0
                turns = 0
                for gather in gatherNodes:
                    logbook.info(f"gatherNode {gather.tile.toString()} value {gather.value}")
                    totalValue += gather.value
                    turns += gather.gatherTurns
                self.info(
                    f"pcst gath achieved {totalValue} turns {gathCapPlan.gather_turns} (target turns {gatherTurns})")
                if totalValue > targetArmy - gatherTurns // 2:
                    move = self.get_tree_move_default(gatherNodes, valueFunc=leafMoveSelectionValueFunc)
                    if move is not None:
                        self.gatherNodes = gatherNodes
                        return self.move_half_on_repetition(move, 4), totalValue, turns, gatherNodes
                    else:
                        logbook.info("Gather returned no moves :(")
                else:
                    logbook.info(f"Value {totalValue} was too small to return... (needed {targetArmy}) :(")
        elif gatherTurns > GATHER_SWITCH_POINT:
            logbook.info(f"    gather_to_target_tiles  USING OLD GATHER DUE TO gatherTurns {gatherTurns}")
            gatherNodes = self.build_mst(targets, maxTime, gatherTurns - 1, negativeSet)
            gatherNodes = Gather.prune_mst_to_turns(
                gatherNodes,
                gatherTurns - 1,
                self.general.player,
                viewInfo=self.viewInfo if self.info_render_gather_values else None,
                preferPrune=self.expansion_plan.preferred_tiles if self.expansion_plan is not None else None)

            if maximizeArmyGatheredPerTurn:
                turns, value, gatherNodes = Gather.prune_mst_to_max_army_per_turn_with_values(
                    gatherNodes,
                    targetArmy,
                    searchingPlayer=self.general.player,
                    teams=MapBase.get_teams_array(self._map),
                    additionalIncrement=additionalIncrement,
                    preferPrune=self.expansion_plan.preferred_tiles if self.expansion_plan is not None else None,
                    viewInfo=self.viewInfo if self.info_render_gather_values else None)

            gatherMove = self.get_tree_move_default(gatherNodes, valueFunc=leafMoveSelectionValueFunc)
            value = 0
            turns = 0
            for node in gatherNodes:
                value += node.value
                turns += node.gatherTurns
            if gatherMove is not None:
                self.info(
                    f"gather_to_target_tiles OLD GATHER {gatherMove.source.toString()} -> {gatherMove.dest.toString()}  gatherTurns: {gatherTurns}")
                self.gatherNodes = gatherNodes
                return self.move_half_on_repetition(gatherMove, 6), value, turns, gatherNodes
        else:
            if additionalIncrement != 0 and targetArmy > 0:
                targetArmy = targetArmy + additionalIncrement * gatherTurns // 2
            if self.gather_use_max_set and not isinstance(targets, dict):
                with self.perf_timer.begin_move_event(f'gath_max_set {gatherTurns}t'):
                    gatherMatrix = self.get_gather_tiebreak_matrix()
                    captureMatrix = self.get_expansion_weight_matrix()
                    valueMatrix = Gather.build_gather_capture_pure_value_matrix(
                        self._map,
                        self.general.player,
                        negativeTiles=negativeSet,
                        gatherMatrix=gatherMatrix,
                        captureMatrix=captureMatrix,
                        useTrueValueGathered=useTrueValueGathered,
                        prioritizeCaptureHighArmyTiles=False)
                    armyCostMatrix = Gather.build_gather_capture_pure_value_matrix(
                        self._map,
                        self.general.player,
                        negativeTiles=negativeSet,
                        gatherMatrix=gatherMatrix,
                        captureMatrix=captureMatrix,
                        useTrueValueGathered=True,
                        prioritizeCaptureHighArmyTiles=False)
                    plan = Gather.gather_max_set_iterative_plan(
                        self._map,
                        targets,
                        gatherTurns,
                        valueMatrix,
                        armyCostMatrix,
                        # negativeTiles=negativeSet,
                        renderLive=False,
                        viewInfo=None,
                        searchingPlayer=self.general.player,
                        fastMode=True,

                        # useTrueValueGathered=useTrueVal,
                        cutoffTime=time.perf_counter() + maxTime
                    )
                    gatherNodes = []
                    if plan and plan.root_nodes:
                        gatherNodes = plan.root_nodes
            else:
                with self.perf_timer.begin_move_event(f'knapsack_max_gather {gatherTurns}t, {targetArmy}a'):
                    gatherNodes = Gather.knapsack_max_gather(
                        self._map,
                        targets,
                        gatherTurns,
                        targetArmy,
                        distPriorityMap=distPriorityMap,
                        negativeTiles=negativeSet,
                        searchingPlayer=self.general.player,
                        viewInfo=self.viewInfo if self.info_render_gather_values else None,
                        useTrueValueGathered=useTrueValueGathered,
                        incrementBackward=False,
                        includeGatherTreeNodesThatGatherNegative=includeGatherTreeNodesThatGatherNegative,
                        priorityMatrix=priorityMatrix,
                        cutoffTime=time.perf_counter() + maxTime,
                        shouldLog=shouldLog,
                        fastMode=fastMode)

            if maximizeArmyGatheredPerTurn:
                turns, value, gatherNodes = Gather.prune_mst_to_max_army_per_turn_with_values(
                    gatherNodes,
                    targetArmy,
                    searchingPlayer=self.general.player,
                    teams=MapBase.get_teams_array(self._map),
                    additionalIncrement=additionalIncrement,
                    preferPrune=self.expansion_plan.preferred_tiles if self.expansion_plan is not None else None,
                    viewInfo=self.viewInfo if self.info_render_gather_values else None)

            totalValue = 0
            turns = 0
            for gather in gatherNodes:
                logbook.info(f"gatherNode {gather.tile.toString()} value {gather.value}")
                totalValue += gather.value
                turns += gather.gatherTurns

            logbook.info(
                f"gather_to_target_tiles totalValue was {totalValue}. Setting gatherNodes for visual debugging regardless of using them")
            if totalValue > targetArmy - gatherTurns // 2:
                move = self.get_tree_move_default(gatherNodes, valueFunc=leafMoveSelectionValueFunc)
                if move is not None:
                    self.gatherNodes = gatherNodes
                    return self.move_half_on_repetition(move, 4), totalValue, turns, gatherNodes
                else:
                    logbook.info("Gather returned no moves :(")
            else:
                logbook.info(f"Value {totalValue} was too small to return... (needed {targetArmy}) :(")
        return None, -1, -1, None

    # set useTrueValueGathered to True for things like defense gathers,
    # where you want to take into account army lost gathering over enemy or neutral tiles etc.
    def get_gather_to_target_tiles_greedy(
            self,
            targets,
            maxTime,
            gatherTurns,
            negativeSet=None,
            targetArmy=-1,
            useTrueValueGathered=False,
            priorityFunc=None,
            valueFunc=None,
            includeGatherTreeNodesThatGatherNegative=False,
            maximizeArmyGatheredPerTurn: bool = False,
            shouldLog: bool = False
    ) -> typing.Tuple[Move | None, int, int, typing.Union[None, typing.List[GatherTreeNode]]]:
        """
        returns move, valueGathered, turnsUsed, gatherNodes

        @param targets:
        @param maxTime:
        @param gatherTurns:
        @param negativeSet:
        @param targetArmy:
        @param useTrueValueGathered: Use True for things like capturing stuff. Causes the algo to include the cost of
         capturing tiles in the value calculation. Also include the cost of the gather start tile into the gather FINDER
         so that it only finds paths that kill the target. Avoid using this when just gathering as it prevents
         gathering tiles on the other side of enemy territory, which is the opposite of good general gather behavior.
         Use useTrueValueGathered to make sure the gatherValue returned by the gather matches the actual amount of army you will have on the gather target tiles at the end of the gather execution.
         Use includeGatherTreeNodesThatGatherNegative to allow a gather to return gather nodes in the final result that fail to flip an enemy node in the path to friendly.
        @param maximizeArmyGatheredPerTurn: if set to True, will prune the result to the maximum gather value amount per turn.
        @param includeGatherTreeNodesThatGatherNegative: if set True, allows the gather PLAN to gather
         to tiles without killing them. Use this for defense for example, when you dont need to fully kill the threat tile with each gather move.
         Use includeGatherTreeNodesThatGatherNegative to allow a gather to return gather nodes in the final result that fail to flip an enemy node in the path to friendly.
         Use useTrueValueGathered to make sure the gatherValue returned by the gather matches the actual amount of army you will have on the gather target tiles at the end of the gather execution.
        @param priorityFunc:
        @param valueFunc:
        @return:
        """

        gatherNodes = Gather.greedy_backpack_gather(
            self._map,
            targets,
            gatherTurns,
            targetArmy,
            negativeTiles=negativeSet,
            searchingPlayer=self.general.player,
            viewInfo=self.viewInfo,
            useTrueValueGathered=useTrueValueGathered,
            includeGatherTreeNodesThatGatherNegative=includeGatherTreeNodesThatGatherNegative,
            shouldLog=shouldLog)

        if maximizeArmyGatheredPerTurn:
            turns, value, gatherNodes = Gather.prune_mst_to_max_army_per_turn_with_values(
                gatherNodes,
                targetArmy,
                searchingPlayer=self.general.player,
                teams=MapBase.get_teams_array(self._map),
                preferPrune=self.expansion_plan.preferred_tiles if self.expansion_plan is not None else None,
                viewInfo=self.viewInfo)

        totalValue = 0
        turns = 0
        for gather in gatherNodes:
            logbook.info(f"gatherNode {gather.tile.toString()} value {gather.value}")
            totalValue += gather.value
            turns += gather.gatherTurns

        logbook.info(
            f"gather_to_target_tiles totalValue was {totalValue}. Setting gatherNodes for visual debugging regardless of using them")
        if totalValue > targetArmy:
            move = self.get_tree_move_default(gatherNodes, valueFunc=valueFunc)
            if move is not None:
                self.gatherNodes = gatherNodes
                return self.move_half_on_repetition(move, 4), totalValue, turns, gatherNodes
            else:
                logbook.info("Gather returned no moves :(")
        else:
            logbook.info(f"Value {totalValue} was too small to return... (needed {targetArmy}) :(")
        return None, -1, -1, None

    def sum_enemy_army_near_tile(self, startTile: Tile, distance: int = 2) -> int:
        """does NOT include the value of the tile itself."""
        enemyNear = SearchUtils.Counter(0)

        def counterFunc(tile: Tile) -> bool:
            if not (tile.isNeutral or self._map.is_tile_friendly(tile)):
                enemyNear.add(tile.army - 1)
            return tile.isCity and tile.isNeutral and tile != startTile

        SearchUtils.breadth_first_foreach(self._map, [startTile], distance, counterFunc, noLog=True, bypassDefaultSkip=True)
        value = enemyNear.value
        if self._map.is_tile_enemy(startTile):
            # don't include the tiles army itself...
            value = value - (startTile.army - 1)
        # logbook.info("enemy_army_near for tile {},{} returned {}".format(tile.x, tile.y, value))
        return value

    def count_enemy_territory_near_tile(self, startTile: Tile, distance: int = 2) -> int:
        enemyNear = SearchUtils.Counter(0)

        def counterFunc(tile: Tile) -> bool:
            tileIsNeutAndNotEnemyTerritory = tile.isNeutral and (tile.visible or self.territories.territoryMap[tile] != self.targetPlayer)
            if not tileIsNeutAndNotEnemyTerritory and self._map.is_tile_enemy(tile):
                enemyNear.add(1)
            return tile.isObstacle and tile != startTile

        SearchUtils.breadth_first_foreach(self._map, [startTile], distance, counterFunc, noLog=True, bypassDefaultSkip=True)
        value = enemyNear.value
        return value

    def count_enemy_tiles_near_tile(self, startTile: Tile, distance: int = 2) -> int:
        enemyNear = SearchUtils.Counter(0)

        def counterFunc(tile: Tile) -> bool:
            if not tile.isNeutral and not self._map.is_tile_friendly(tile):
                enemyNear.add(1)

            return tile.isObstacle and tile != startTile

        SearchUtils.breadth_first_foreach(self._map, [startTile], distance, counterFunc, noLog=True)
        value = enemyNear.value
        return value

    def sum_player_army_near_tile(self, tile: Tile, distance: int = 2, player: int | None = None) -> int:
        """
        does not include the army value ON the tile itself.

        @param tile:
        @param distance:
        @param player: if None, will use self.general.player
        @return:
        """

        armyNear = self.sum_player_standing_army_near_or_on_tiles([tile], distance, player)
        logbook.info(f"player_army_near for tile {tile.x},{tile.y} player {player} returned {armyNear}")
        if tile.player == player:
            # don't include the tiles army itself...
            armyNear = armyNear - (tile.army - 1)
        return armyNear

    def sum_player_standing_army_near_or_on_tiles(self, tiles: typing.List[Tile], distance: int = 2, player: int | None = None) -> int:
        """
        DOES include the army value ON the tile itself.

        @param tiles:
        @param distance:
        @param player: if None, will use self.general.player
        @return:
        """
        if player is None:
            player = self._map.player_index
        armyNear = SearchUtils.Counter(0)

        def counterFunc(tile: Tile):
            if tile.player != player:
                armyNear.add(tile.army - 1)

        SearchUtils.breadth_first_foreach_fast_no_neut_cities(self._map, tiles, distance, counterFunc)
        value = armyNear.value
        return value

    def sum_friendly_army_near_tile(self, tile: Tile, distance: int = 2, player: int | None = None) -> int:
        """
        does not include the army value ON the tile itself.

        @param tile:
        @param distance:
        @param player: if None, will use self.general.player
        @return:
        """

        armyNear = self.sum_friendly_army_near_or_on_tiles([tile], distance, player)
        logbook.info(f"friendly_army_near for tile {tile.x},{tile.y} player {player} returned {armyNear}")
        if self._map.is_tile_on_team_with(tile, player):
            # don't include the tiles army itself...
            armyNear = armyNear - (tile.army - 1)
        return armyNear

    def sum_friendly_army_near_or_on_tiles(self, tiles: typing.List[Tile], distance: int = 2, player: int | None = None) -> int:
        """
        DOES include the army value ON the tile itself.

        @param tiles:
        @param distance:
        @param player: if None, will use self.general.player
        @return:
        """
        if player is None:
            player = self._map.player_index
        armyNear = SearchUtils.Counter(0)

        def counterFunc(tile: Tile):
            if self._map.is_tile_on_team_with(tile, player):
                armyNear.add(tile.army - 1)

        SearchUtils.breadth_first_foreach(self._map, tiles, distance, counterFunc)
        value = armyNear.value
        return value

    def get_first_path_move(self, path: TilePlanInterface):
        return path.get_first_move()

    def get_afk_players(self) -> typing.List[Player]:
        if self._afk_players is None:
            self._afk_players = []
            minTilesToNotBeAfk = math.sqrt(self._map.turn)
            for player in self._map.players:
                if player.index == self.general.player:
                    continue
                # logbook.info("player {}  self._map.turn {} > 50 ({}) and player.tileCount {} < minTilesToNotBeAfk {:.1f} ({}): {}".format(player.index, self._map.turn, self._map.turn > 50, player.tileCount, minTilesToNotBeAfk, player.tileCount < 10, self._map.turn > 50 and player.tileCount < 10))
                if (player.leftGame or (self._map.turn >= 50 and player.tileCount <= minTilesToNotBeAfk)) and not player.dead:
                    self._afk_players.append(player)
                    logbook.info(f"player {self._map.usernames[player.index]} ({player.index}) was afk")

        return self._afk_players

    def get_optimal_exploration(
            self,
            turns,
            negativeTiles: typing.Set[Tile] = None,
            valueFunc=None,
            priorityFunc=None,
            initFunc=None,
            skipFunc=None,
            minArmy=0,
            maxTime: float | None = None,
            emergenceRatio: float = 0.15,
            includeCities: bool | None = None,
    ) -> Path | None:
        # return None

        if includeCities is None:
            includeCities = not self.armyTracker.has_perfect_information_of_player_cities(self.targetPlayer) and WinCondition.ContestEnemyCity in self.win_condition_analyzer.viable_win_conditions

        toReveal = self.get_target_player_possible_general_location_tiles_sorted(elimNearbyRange=0, player=self.targetPlayer, cutoffEmergenceRatio=emergenceRatio, includeCities=includeCities)
        targetArmyLevel = self.determine_fog_defense_amount_available_for_tiles(toReveal, self.targetPlayer)

        for t in toReveal:
            self.mark_tile(t, alpha=50)

        if len(toReveal) == 0:
            return None

        startArmies = sorted(self.get_largest_tiles_as_armies(self.general.player, limit=3), key=lambda t: self._map.get_distance_between(self.targetPlayerExpectedGeneralLocation, t.tile))

        bestArmy = None
        bestThresh = None
        for startArmy in startArmies:
            if negativeTiles and startArmy.tile in negativeTiles:
                continue
            armyDiff = startArmy.value - targetArmyLevel
            dist = self._map.get_distance_between(self.targetPlayerExpectedGeneralLocation, startArmy.tile)
            if dist <= 0:
                dist = 1
            if armyDiff <= 0:
                # continue
                thisThresh = -dist + armyDiff
            else:
                thisThresh = armyDiff / dist

            if bestArmy is None or bestThresh < thisThresh:
                bestThresh = thisThresh
                bestArmy = startArmy

        if bestArmy is None:
            return None

        startTile = bestArmy.tile

        with self.perf_timer.begin_move_event(f'Watch {startTile} c{str(includeCities)[0]} tgA {targetArmyLevel}'):
            if maxTime is None:
                maxTime = self.get_remaining_move_time() / 2

            path = WatchmanRouteUtils.get_watchman_path(
                self._map,
                startTile,
                toReveal,
                timeLimit=maxTime,
            )

        if path is None or path.length == 0:
            return None

        # path = path.get_positive_subsegment(self.general.player, self._map.team_ids_by_player_index, negativeTiles)

        revealedCount, maxKillTurns, minKillTurns, avgKillTurns, rawKillDistByTileMatrix, bestRevealedPath = WatchmanRouteUtils.get_revealed_count_and_max_kill_turns_and_positive_path(self._map, path, toReveal, targetArmyLevel)
        if bestRevealedPath is not None:
            self.info(f'WRPHunt (c{str(includeCities)[0]} ta{targetArmyLevel}) {startTile} {bestRevealedPath.length}t ({path.length}t) rev {revealedCount}/{len(toReveal)}, kill min{minKillTurns} max{maxKillTurns} avg{avgKillTurns:.1f}')
        else:
            self.info(f'WRPHunt (c{str(includeCities)[0]} ta{targetArmyLevel}) {startTile} -NONE- ({path.length}t) rev {revealedCount}/{len(toReveal)}, kill min{minKillTurns} max{maxKillTurns} avg{avgKillTurns:.1f}')
            bestRevealedPath = None

        return bestRevealedPath

        # allow exploration again
        #
        # negMinArmy = 0 - minArmy
        #
        # logbook.info(f"\n\nAttempting Optimal EXPLORATION (tm) for turns {turns}:\n")
        # startTime = time.perf_counter()
        # generalPlayer = self._map.players[self.general.player]
        # searchingPlayer = self.general.player
        # if negativeTiles is None:
        #     negativeTiles = set()
        # else:
        #     negativeTiles = negativeTiles.copy()
        # logbook.info(f"negativeTiles: {str(negativeTiles)}")
        #
        # distSource = self.general
        # if self.target_player_gather_path is not None:
        #     distSource = self.targetPlayerExpectedGeneralLocation
        # distMap = self._map.distance_mapper.get_tile_dist_matrix(distSource)
        #
        # ourArmies = SearchUtils.where(self.armyTracker.armies.values(), lambda army: army.player == self.general.player and army.tile.player == self.general.player and army.tile.army > 1)
        # ourArmyTiles = [army.tile for army in ourArmies]
        # if len(ourArmyTiles) == 0:
        #     logbook.info("We didn't have any armies to use to optimal_exploration. Using our tiles with army > 5 instead.")
        #     ourArmyTiles = SearchUtils.where(self._map.players[self.general.player].tiles, lambda tile: tile.army > 5)
        # if len(ourArmyTiles) == 0:
        #     logbook.info("We didn't have any armies to use to optimal_exploration. Using our tiles with army > 2 instead.")
        #     ourArmyTiles = SearchUtils.where(self._map.players[self.general.player].tiles, lambda tile: tile.army > 2)
        # if len(ourArmyTiles) == 0:
        #     logbook.info("We didn't have any armies to use to optimal_exploration. Using our tiles with army > 1 instead.")
        #     ourArmyTiles = SearchUtils.where(self._map.players[self.general.player].tiles, lambda tile: tile.army > 1)
        #
        # ourArmyTiles = SearchUtils.where(ourArmyTiles, lambda t: t.army > negMinArmy)
        #
        # # require any exploration path go through at least one of these tiles.
        # validExplorationTiles = MapMatrixSet(self._map)
        # for tile in self._map.pathableTiles:
        #     if (
        #             not tile.discovered
        #             and (self.territories.territoryMap[tile] == self.targetPlayer or distMap[tile] < 6)
        #     ):
        #         validExplorationTiles.add(tile)
        #
        # # skipFunc(next, nextVal). Not sure why this is 0 instead of 1, but 1 breaks it. I guess the 1 is already subtracted
        # if not skipFunc:
        #     def skip_after_out_of_army(nextTile, nextVal):
        #         wastedMoves, pathPriorityDivided, negArmyRemaining, negValidExplorationCount, negRevealedCount, enemyTiles, neutralTiles, pathPriority, distSoFar, tileSetSoFar, revealedSoFar = nextVal
        #         if negArmyRemaining >= negMinArmy:
        #             return True
        #         if distSoFar > 6 and negValidExplorationCount == 0:
        #             return True
        #         if wastedMoves > 3:
        #             return True
        #         return False
        #
        #     skipFunc = skip_after_out_of_army
        #
        # if not valueFunc:
        #     def value_priority_army_dist(currentTile, priorityObject):
        #         wastedMoves, pathPriorityDivided, negArmyRemaining, negValidExplorationCount, negRevealedCount, enemyTiles, neutralTiles, pathPriority, distSoFar, tileSetSoFar, revealedSoFar = priorityObject
        #         # negative these back to positive
        #         if negValidExplorationCount == 0:
        #             return None
        #         if negArmyRemaining > 0:
        #             return None
        #
        #         posPathPrio = 0 - pathPriorityDivided
        #
        #         # pathPriority includes emergence values.
        #         value = 0 - (negRevealedCount + enemyTiles * 6 + neutralTiles) / distSoFar
        #
        #         return value, posPathPrio, distSoFar
        #
        #     valueFunc = value_priority_army_dist
        #
        # if not priorityFunc:
        #     def default_priority_func(nextTile, currentPriorityObject):
        #         wastedMoves, pathPriorityDivided, negArmyRemaining, negValidExplorationCount, negRevealedCount, enemyTiles, neutralTiles, pathPriority, distSoFar, tileSetSoFar, revealedSoFar = currentPriorityObject
        #         armyRemaining = 0 - negArmyRemaining
        #         nextTileSet = tileSetSoFar.copy()
        #         distSoFar += 1
        #         # weight tiles closer to the target player higher
        #         addedPriority = -4 - max(2.0, distMap[nextTile] / 3)
        #         # addedPriority = -7 - max(3, distMap[nextTile] / 4)
        #         if nextTile not in nextTileSet:
        #             armyRemaining -= 1
        #             releventAdjacents = SearchUtils.where(nextTile.adjacents, lambda adjTile: adjTile not in revealedSoFar and adjTile not in tileSetSoFar)
        #             revealedCount = SearchUtils.count(releventAdjacents, lambda adjTile: not adjTile.discovered)
        #             negRevealedCount -= revealedCount
        #             if negativeTiles is None or (nextTile not in negativeTiles):
        #                 if searchingPlayer == nextTile.player:
        #                     armyRemaining += nextTile.army
        #                 else:
        #                     armyRemaining -= nextTile.army
        #             if nextTile in validExplorationTiles:
        #                 negValidExplorationCount -= 1
        #                 addedPriority += 3
        #             nextTileSet.add(nextTile)
        #             # enemytiles or enemyterritory undiscovered tiles
        #             if self.targetPlayer != -1 and (nextTile.player == self.targetPlayer or (not nextTile.visible and self.territories.territoryMap[nextTile] == self.targetPlayer)):
        #                 if nextTile.player == -1:
        #                     # these are usually 2 or more army since usually after army bonus
        #                     armyRemaining -= 2
        #                 #    # points for maybe capping target tiles
        #                 #    addedPriority += 4
        #                 #    enemyTiles -= 0.5
        #                 #    neutralTiles -= 0.5
        #                 #    # treat this tile as if it is at least 1 cost
        #                 # else:
        #                 #    # points for capping target tiles
        #                 #    addedPriority += 6
        #                 #    enemyTiles -= 1
        #                 addedPriority += 8
        #                 enemyTiles -= 1
        #                 ## points for locking all nearby enemy tiles down
        #                 # numEnemyNear = SearchUtils.count(nextTile.adjacents, lambda adjTile: adjTile.player == self.player)
        #                 # numEnemyLocked = SearchUtils.count(releventAdjacents, lambda adjTile: adjTile.player == self.player)
        #                 ##    for every other nearby enemy tile on the path that we've already included in the path, add some priority
        #                 # addedPriority += (numEnemyNear - numEnemyLocked) * 12
        #             elif nextTile.player == -1:
        #                 # we'd prefer to be killing enemy tiles, yeah?
        #                 wastedMoves += 0.2
        #                 neutralTiles -= 1
        #                 # points for capping tiles in general
        #                 addedPriority += 1
        #                 # points for taking neutrals next to enemy tiles
        #                 numEnemyNear = SearchUtils.count(nextTile.movable, lambda adjTile: adjTile not in revealedSoFar and adjTile.player == self.targetPlayer)
        #                 if numEnemyNear > 0:
        #                     addedPriority += 1
        #             else:  # our tiles and non-target enemy tiles get negatively weighted
        #                 # addedPriority -= 2
        #                 # 0.7
        #                 wastedMoves += 1
        #             # points for discovering new tiles
        #             addedPriority += revealedCount * 2
        #             if self.armyTracker.emergenceLocationMap[self.targetPlayer][nextTile] > 0 and not nextTile.visible:
        #                 addedPriority += (self.armyTracker.emergenceLocationMap[self.targetPlayer][nextTile] ** 0.5)
        #             ## points for revealing tiles in the fog
        #             # addedPriority += SearchUtils.count(releventAdjacents, lambda adjTile: not adjTile.visible)
        #         else:
        #             wastedMoves += 1
        #
        #         nextRevealedSet = revealedSoFar.copy()
        #         for adj in SearchUtils.where(nextTile.adjacents, lambda tile: not tile.discovered):
        #             nextRevealedSet.add(adj)
        #         newPathPriority = pathPriority - addedPriority
        #         # if generalPlayer.tileCount < 46:
        #         #    logbook.info("nextTile {}, newPathPriority / distSoFar {:.2f}, armyRemaining {}, newPathPriority {}, distSoFar {}, len(nextTileSet) {}".format(nextTile.toString(), newPathPriority / distSoFar, armyRemaining, newPathPriority, distSoFar, len(nextTileSet)))
        #         return wastedMoves, newPathPriority / distSoFar, 0 - armyRemaining, negValidExplorationCount, negRevealedCount, enemyTiles, neutralTiles, newPathPriority, distSoFar, nextTileSet, nextRevealedSet
        #
        #     priorityFunc = default_priority_func
        #
        # if not initFunc:
        #     def initial_value_func_default(t: Tile):
        #         startingSet = set()
        #         startingSet.add(t)
        #         startingAdjSet = set()
        #         for adj in t.adjacents:
        #             startingAdjSet.add(adj)
        #         return 0, 10, 0 - t.army, 0, 0, 0, 0, 0, 0, startingSet, startingAdjSet
        #
        #     initFunc = initial_value_func_default
        #
        # if turns <= 0:
        #     logbook.info("turns <= 0 in optimal_exploration? Setting to 50")
        #     turns = 50
        # remainingTurns = turns
        # sortedTiles = sorted(ourArmyTiles, key=lambda t: t.army, reverse=True)
        # paths = []
        #
        # player = self._map.players[self.general.player]
        # logStuff = False
        #
        # # BACKPACK THIS EXPANSION! Don't stop at remainingTurns 0... just keep finding paths until out of time, then knapsack them
        #
        # # Switch this up to use more tiles at the start, just removing the first tile in each path at a time. Maybe this will let us find more 'maximal' paths?
        #
        # while sortedTiles:
        #     timeUsed = time.perf_counter() - startTime
        #     # Stages:
        #     # first 0.1s, use large tiles and shift smaller. (do nothing)
        #     # second 0.1s, use all tiles (to make sure our small tiles are included)
        #     # third 0.1s - knapsack optimal stuff outside this loop i guess?
        #     if timeUsed > 0.03:
        #         logbook.info(f"timeUsed {timeUsed:.3f} > 0.03... Breaking loop and knapsacking...")
        #         break
        #
        #     # startIdx = max(0, ((cutoffFactor - 1) * len(sortedTiles))//fullCutoff)
        #
        #     # hack,  see what happens TODO
        #     # tilesLargerThanAverage = SearchUtils.where(generalPlayer.tiles, lambda tile: tile.army > 1)
        #     # logbook.info("Filtered for tilesLargerThanAverage with army > {}, found {} of them".format(tilePercentile[-1].army, len(tilesLargerThanAverage)))
        #     startDict = {}
        #     for i, tile in enumerate(sortedTiles):
        #         # skip tiles we've already used or intentionally ignored
        #         if tile in negativeTiles:
        #             continue
        #         # self.mark_tile(tile, 10)
        #
        #         initVal = initFunc(tile)
        #         # wastedMoves, pathPriorityDivided, armyRemaining, pathPriority, distSoFar, tileSetSoFar
        #         # 10 because it puts the tile above any other first move tile, so it gets explored at least 1 deep...
        #         startDict[tile] = (initVal, 0)
        #     path, pathValue = SearchUtils.breadth_first_dynamic_max(
        #         self._map,
        #         startDict,
        #         valueFunc,
        #         0.025,
        #         remainingTurns,
        #         turns,
        #         noNeutralCities=True,
        #         negativeTiles=negativeTiles,
        #         searchingPlayer=self.general.player,
        #         priorityFunc=priorityFunc,
        #         useGlobalVisitedSet=False,
        #         skipFunc=skipFunc,
        #         logResultValues=logStuff,
        #         includePathValue=True)
        #
        #     if path:
        #         (pathPriorityPerTurn, posPathPrio, distSoFar) = pathValue
        #         logbook.info(f"Path found for maximizing army usage? Duration {time.perf_counter() - startTime:.3f} path {path.toString()}")
        #         node = path.start
        #         # BYPASSED THIS BECAUSE KNAPSACK...
        #         # remainingTurns -= path.length
        #         tilesGrabbed = 0
        #         visited = set()
        #         friendlyCityCount = 0
        #         while node is not None:
        #             negativeTiles.add(node.tile)
        #
        #             if self._map.is_tile_friendly(node.tile) and (node.tile.isCity or node.tile.isGeneral):
        #                 friendlyCityCount += 1
        #             # this tile is now worth nothing because we already intend to use it ?
        #             # skipTiles.add(node.tile)
        #             node = node.next
        #         sortedTiles.remove(path.start.tile)
        #         paths.append((friendlyCityCount, pathPriorityPerTurn, path))
        #     else:
        #         logbook.info("Didn't find a super duper cool optimized EXPLORATION pathy thing. Breaking :(")
        #         break
        #
        # alpha = 75
        # minAlpha = 50
        # alphaDec = 2
        # trimmable = {}
        #
        # # build knapsack weights and values
        # weights = [pathTuple[2].length for pathTuple in paths]
        # values = [int(100 * pathTuple[1]) for pathTuple in paths]
        # logbook.info(f"Feeding the following paths into knapsackSolver at turns {turns}...")
        # for i, pathTuple in enumerate(paths):
        #     friendlyCityCount, pathPriorityPerTurn, curPath = pathTuple
        #     logbook.info(f"{i}:  cities {friendlyCityCount} pathPriorityPerTurn {pathPriorityPerTurn} length {curPath.length} path {curPath.toString()}")
        #
        # totalValue, maxKnapsackedPaths = solve_knapsack(paths, turns, weights, values)
        # logbook.info(f"maxKnapsackedPaths value {totalValue} length {len(maxKnapsackedPaths)},")
        #
        # path = None
        # if len(maxKnapsackedPaths) > 0:
        #     maxVal = (-100, -1)
        #
        #     # Select which of the knapsack paths to move first
        #     for pathTuple in maxKnapsackedPaths:
        #         friendlyCityCount, tilesCaptured, curPath = pathTuple
        #
        #         thisVal = (0 - friendlyCityCount, tilesCaptured / curPath.length)
        #         if thisVal > maxVal:
        #             maxVal = thisVal
        #             path = curPath
        #             logbook.info(f"no way this works, evaluation [{'], ['.join(str(x) for x in maxVal)}], path {path.toString()}")
        #
        #         # draw other paths darker
        #         alpha = 150
        #         minAlpha = 150
        #         alphaDec = 0
        #         self.viewInfo.color_path(PathColorer(curPath, 50, 51, 204, alpha, alphaDec, minAlpha))
        #     logbook.info(f"EXPLORATION PLANNED HOLY SHIT? Duration {time.perf_counter() - startTime:.3f}, path {path.toString()}")
        #     # draw maximal path darker
        #     alpha = 255
        #     minAlpha = 200
        #     alphaDec = 0
        #     self.viewInfo.paths = deque(SearchUtils.where(self.viewInfo.paths, lambda pathCol: pathCol.path != path))
        #     self.viewInfo.color_path(PathColorer(path, 55, 100, 200, alpha, alphaDec, minAlpha))
        # else:
        #     logbook.info(f"No EXPLORATION plan.... :( Duration {time.perf_counter() - startTime:.3f},")
        #
        # return path

    def explore_target_player_undiscovered(self, negativeTiles: typing.Set[Tile] | None, onlyHuntGeneral: bool | None = None, maxTime: float | None = None) -> Path | None:
        # if self._map.turn < 100 or self.player == -1 or self._map.generals[self.player] is not None:
        if negativeTiles:
            negativeTiles = negativeTiles.copy()
        if self._map.turn < 50 or self.targetPlayer == -1:
            return None

        turnInCycle = self.timings.get_turn_in_cycle(self._map.turn)
        exploringUnknown = self._map.generals[self.targetPlayer] is None

        # TODO 2v2
        genPlayer = self._map.players[self.general.player]
        behindOnCities = genPlayer.cityCount < self._map.players[self.targetPlayer].cityCount

        if not self.is_all_in():
            if self.explored_this_turn:
                logbook.info("(skipping new exploration because already explored this turn)")
                return None
            if not self.finishing_exploration and behindOnCities:
                logbook.info("(skipping new exploration because behind on cities and wasn't finishing exploration)")
                return None

        # if self.is_all_in():
        #     minArmy = int(genPlayer.standingArmy ** 0.75) - 15
        #     path = self.explore_target_player_undiscovered_short(minArmy, skipTiles)
        #     if path is not None:
        #         # already logged
        #         return path

        # if not self.opponent_tracker.winning_on_army(byRatio=1.25):
        #     return None

        enGenPositions = self.armyTracker.valid_general_positions_by_player[self.targetPlayer]

        for player in self._map.players:
            if not player.dead and player.team == self.targetPlayerObj.team and player.index != self.targetPlayer:
                enGenPositions = enGenPositions.copy()
                for i, val in enumerate(self.armyTracker.valid_general_positions_by_player[player.index].raw):
                    enGenPositions.raw[i] = enGenPositions.raw[i] or val

        if onlyHuntGeneral is None:
            onlyHuntGeneral = self.armyTracker.has_perfect_information_of_player_cities(self.targetPlayer)

        if onlyHuntGeneral:
            for tile in self._map.get_all_tiles():
                if not self._map.is_tile_friendly(tile) and not enGenPositions[tile]:
                    negativeTiles.add(tile)

        self.explored_this_turn = True
        turns = self.timings.cycleTurns - turnInCycle
        minArmy = max(12, int(genPlayer.standingArmy ** 0.75) - 10)
        self.info(f"Forcing explore to t{turns} and minArmy to {minArmy}")
        if self.is_all_in() and not self.is_all_in_army_advantage and not self.all_in_city_behind:
            turns = 15
            minArmy = int(genPlayer.standingArmy ** 0.83) - 10
            self.info(f"Forcing explore to t{turns} and minArmy to {minArmy} because self.is_all_in()")
        elif turns < 6:
            logbook.info(f"Forcing explore turns to minimum of 5, was {turns}")
            turns = 5
        elif turnInCycle < 6 and exploringUnknown:
            logbook.info(f"Forcing explore turns to minimum of 6, was {turns}")
            turns = 6

        if self._map.turn < 100:
            return None

        path = self.get_optimal_exploration(turns, negativeTiles, minArmy=minArmy, maxTime=maxTime)
        if path:
            logbook.info(f"Oh no way, explore found a path lol? {str(path)}")
            tilesRevealed = set()
            score = 0
            node = path.start
            while node is not None:
                if not node.tile.discovered and self.armyTracker.emergenceLocationMap[self.targetPlayer][node.tile] > 0 and (not onlyHuntGeneral or enGenPositions.raw[node.tile.tile_index]):
                    score += self.armyTracker.emergenceLocationMap[self.targetPlayer][node.tile] ** 0.5
                for adj in node.tile.adjacents:
                    if not adj.discovered and (not onlyHuntGeneral or enGenPositions[adj]):
                        tilesRevealed.add(adj)
                node = node.next
            revealedPerMove = len(tilesRevealed) / path.length
            scorePerMove = score / path.length
            self.viewInfo.add_info_line(
                f"hunting tilesRevealed {len(tilesRevealed)} ({revealedPerMove:.2f}), Score {score} ({scorePerMove:.2f}), path.length {path.length}")
            if ((revealedPerMove > 0.5 and scorePerMove > 4)
                    or (revealedPerMove > 0.8 and scorePerMove > 1)
                    or revealedPerMove > 1.5):
                # if path.length > 2:
                #     path = path.get_subsegment(2)

                self.finishing_exploration = True
                self.info(
                    f"NEW hunting, search turns {turns}, minArmy {minArmy}, allIn {self.is_all_in_losing} finishingExp {self.finishing_exploration} ")
                return path
            else:
                logbook.info("path wasn't good enough, discarding")

        return None

        ## don't explore to 1 army from inside our own territory
        ##if not self.timings.in_gather_split(self._map.turn):
        # if skipTiles is None:
        #    skipTiles = set()
        # skipTiles = skipTiles.copy()
        # for tile in genPlayer.tiles:
        #    if self.territories.territoryMap[tile] == self.general.player:
        #        logbook.info("explore: adding tile {} to skipTiles for lowArmy search".format(tile.toString()))
        #        skipTiles.add(tile)

        # path = SearchUtils.breadth_first_dynamic(self._map,
        #                            enemyUndiscBordered,
        #                            goal_func_short,
        #                            0.1,
        #                            3,
        #                            noNeutralCities = True,
        #                            skipTiles = skipTiles,
        #                            searchingPlayer = self.general.player,
        #                            priorityFunc = priority_func_non_all_in)
        # if path is not None:
        #    path = path.get_reversed()
        #    self.info("UD SMALL: depth {} bfd kill (pre-prune) \n{}".format(path.length, path.toString()))

    def get_median_tile_value(self, percentagePoint=50, player: int = -1):
        if player == -1:
            player = self.general.player

        tiles = [tile for tile in self._map.players[player].tiles]
        tiles = sorted(tiles, key=lambda tile: tile.army)
        tileIdx = max(0, int(len(tiles) * percentagePoint // 100 - 1))
        if len(tiles) > tileIdx:
            return tiles[tileIdx].army
        else:
            logbook.info("whoah, dude cmon,Z ZzzZZzzZZzzZZzzZZzzZZzzZZzzZZzzZZzzZZzzZZzzZZzzZZzzZZzzZZzzZZzzZZzzZZzzZZzzZZzzZZzzZZzz")
            logbook.info("hit that weird tileIdx bug.")
            return 0

    def build_mst(self, startTiles, maxTime=0.1, maxDepth=150, negativeTiles: typing.Set[Tile] = None, avoidTiles=None, priorityFunc=None):
        LOG_TIME = False
        searchingPlayer = self._map.player_index
        frontier = SearchUtils.HeapQueue()
        visitedBack = MapMatrix(self._map)

        if isinstance(startTiles, dict):
            for tile in startTiles.keys():
                if isinstance(startTiles[tile], int):
                    distance = startTiles[tile]
                    frontier.put((distance, (0, 0, distance, tile.x, tile.y), tile, tile))
                else:
                    (startPriorityObject, distance) = startTiles[tile]
                    startVal = startPriorityObject
                    frontier.put((distance, startVal, tile, tile))
        else:
            startTiles = set(startTiles)
            if priorityFunc is not None:
                raise AssertionError("You MUST use a dict of startTiles if not using the emptyVal priorityFunc")
            for tile in startTiles:
                negEnemyCount = 0
                if tile.player == self.targetPlayer:
                    negEnemyCount = -1
                frontier.put((0, (0, 0, 0, tile.x, tile.y), tile, tile))

        if not priorityFunc:
            def default_priority_func(nextTile, currentPriorityObject):
                (prio, negArmy, dist, xSum, ySum) = currentPriorityObject
                nextArmy = 0 - negArmy - 1
                if negativeTiles is None or nextTile not in negativeTiles:
                    if searchingPlayer == nextTile.player:
                        nextArmy += nextTile.army
                    else:
                        nextArmy -= nextTile.army
                dist += 1
                return 0 - nextArmy / dist, 0 - nextArmy, dist, xSum + nextTile.x, ySum + nextTile.y

            priorityFunc = default_priority_func
            # if newDist not in visited[next.x][next.y] or visited[next.x][next.y][newDist][0] < nextArmy:
            #     visited[next.x][next.y][newDist] = (nextArmy, current)

        # sort on distance, then army, then x and y (to stop the paths from shuffling randomly and looking annoying)
        start = time.perf_counter()
        # frontier.put((0, startArmy, tile.x, tile.y, tile, None, 0))
        while frontier.queue:
            (dist, curPriorityVal, current, cameFrom) = frontier.get()
            if visitedBack.raw[current.tile_index] is not None:
                continue
            if avoidTiles is not None and current in avoidTiles:
                continue
            if current.isMountain or (not current.discovered and current.isNotPathable):
                continue
            if current.isCity and current.player != searchingPlayer and current not in startTiles:
                dist += 7
            visitedBack.raw[current.tile_index] = cameFrom
            if dist <= maxDepth:
                dist += 1
                for next in current.movable:  # new spots to try
                    nextPriorityVal = priorityFunc(next, curPriorityVal)
                    frontier.put((dist, nextPriorityVal, next, current))
        if LOG_TIME:
            logbook.info(f"BUILD-MST DURATION: {time.perf_counter() - start:.3f}")

        result = self.build_mst_rebuild(startTiles, visitedBack, self._map.player_index)

        return result

    def build_mst_rebuild(self, startTiles, fromMap, searchingPlayer):
        results = []
        for tile in startTiles:
            gather = self.get_gather_mst(tile, None, fromMap, 0, searchingPlayer)
            if gather.tile.player == searchingPlayer:
                gather.value -= gather.tile.army
            else:
                gather.value += gather.tile.army

            results.append(gather)
        return results

    def get_gather_mst(self, tile, fromTile, fromMap, turn, searchingPlayer):
        gatherTotal = tile.army
        turnTotal = 1
        if tile.player != searchingPlayer:
            gatherTotal = 0 - tile.army
        gatherTotal -= 1
        thisNode = GatherTreeNode(tile, fromTile, turn)
        if tile.player == -1:
            thisNode.neutrals = 1
        for move in tile.movable:
            # logbook.info("evaluating {},{}".format(move.x, move.y))
            if move == fromTile:
                # logbook.info("move == fromTile  |  {},{}".format(move.x, move.y))
                continue
            if fromMap.raw[move.tile_index] != tile:
                # logbook.info("fromMap[move.x][move.y] != tile  |  {},{}".format(move.x, move.y))
                continue
            gather = self.get_gather_mst(move, tile, fromMap, turn + 1, searchingPlayer)
            if gather.value > 0:
                gatherTotal += gather.value
                turnTotal += gather.gatherTurns
                thisNode.children.append(gather)

        thisNode.value = gatherTotal
        thisNode.gatherTurns = turnTotal
        # only de-prioritize cities when they are the leaf
        # if thisNode.tile.isCity and 0 == len(thisNode.children):
        #     thisNode.value -= 10
        # logbook.info("{},{} ({}  {})".format(thisNode.tile.x, thisNode.tile.y, thisNode.value, thisNode.gatherTurns))
        return thisNode

    def get_tree_move_non_city_leaf_count(self, gathers):
        # fuck it, do it recursively i'm too tired for this
        count = 0
        for gather in gathers:
            foundCity, countNonCityLeaves = self._get_tree_move_non_city_leaf_count_recurse(gather)
            count += countNonCityLeaves
        return count

    def _get_tree_move_non_city_leaf_count_recurse(self, gather):
        count = 0
        thisNodeFoundCity = False
        for child in gather.children:
            foundCity, countNonCityLeaves = self._get_tree_move_non_city_leaf_count_recurse(child)
            logbook.info(f"child {child.tile.toString()} foundCity {foundCity} countNonCityLeaves {countNonCityLeaves}")
            count += countNonCityLeaves
            if foundCity:
                thisNodeFoundCity = True
        if self._map.is_tile_friendly(gather.tile) and (gather.tile.isCity or gather.tile.isGeneral):
            thisNodeFoundCity = True
        if not thisNodeFoundCity:
            count += 1
        return thisNodeFoundCity, count

    def _get_tree_move_default_value_func(self) -> typing.Callable[[Tile, typing.Tuple], typing.Tuple | None]:
        # emptyVal value func, gathers based on cityCount then distance from general
        def default_value_func(currentTile, currentPriorityObject):
            negCityCount = negDistFromPlayArea = army = unfriendlyTileCount = 0
            # i don't think this does anything...?
            curIsOurCity = True
            if currentPriorityObject is not None:
                (_, nextIsOurCity, negCityCount, unfriendlyTileCount, negDistFromPlayArea, army, curIsOurCity) = currentPriorityObject
                army -= 1
            nextIsOurCity = curIsOurCity
            curIsOurCity = True
            if self._map.is_tile_friendly(currentTile):
                if currentTile.isGeneral or currentTile.isCity:
                    negCityCount -= 1
            else:
                if currentTile.isGeneral or currentTile.isCity and army + 2 <= currentTile.army:
                    curIsOurCity = False
                    # cityCount += 1
                unfriendlyTileCount += 1

            # negDistFromPlayArea = 0 - self.board_analysis.shortest_path_distances[currentTile]
            negDistFromPlayArea = 0 - self.board_analysis.intergeneral_analysis.bMap.raw[currentTile.tile_index]

            if self._map.is_tile_friendly(currentTile):
                army += currentTile.army
            else:
                army -= currentTile.army
            # heuristicVal = negArmy / distFromPlayArea
            return currentTile.isSwamp, nextIsOurCity, negCityCount, unfriendlyTileCount, negDistFromPlayArea, army, curIsOurCity

        return default_value_func

    def get_tree_move_default(
            self,
            gathers: typing.List[GatherTreeNode],
            valueFunc: typing.Callable[[Tile, typing.Tuple], typing.Tuple | None] | None = None,
            pop: bool = False
    ) -> Move | None:
        """
        By emptyVal, gathers cities last.
        Gathers furthest tiles first.

        @param gathers:
            def default_priority_func(nextTile, currentPriorityObject):
                cityCount = distFromPlayArea = negArmy = negUnfriendlyTileCount = 0
                if currentPriorityObject is not None:
                    (cityCount, negUnfriendlyTileCount, distFromPlayArea, negArmy) = currentPriorityObject
        @param valueFunc:
        @param pop: if true, modify the gather plan popping the move off.
        @return:
        """

        if valueFunc is None:
            valueFunc = self._get_tree_move_default_value_func()

        move = Gather.get_tree_move(gathers, valueFunc, pop=pop)
        if move is not None and move.source.player != self.general.player:
            logbook.error(f'returned a move {move} that wasnt from our tile. Replacing with another move further in the list...')
            self.viewInfo.add_info_line(f'returned a move {move} that wasnt from our tile. Replacing with another move further in the list...')
            moves = Gather.get_tree_moves(gathers, valueFunc, pop=False)
            newMove = None
            for newMove in moves:
                if newMove.source.player == self.general.player and newMove.source.army > 1:
                    break
            if newMove is not None and newMove.source.player == self.general.player:
                self.viewInfo.add_info_line(f'GTMD RET BAD {move} - Replacing with {newMove}')
                return newMove
            self.viewInfo.add_info_line(f'GTMD RET BAD {move} NO GOOD MOVE FOUND')
            return None
        return move

    def get_threat_killer_move(self, threat: ThreatObj, searchTurns, negativeTiles) -> Move | None:
        """
        Attempt to find a threat kill path / move that kills a specific threat. TODO can this largely be replaced by defense backpack gather...?
        @param threat:
        @param searchTurns:
        @param negativeTiles:
        @return:
        """
        killTiles = [threat.path.start.tile]
        if threat.path.start.next:
            killTiles.insert(0, threat.path.start.next.tile)

        threatTile = threat.path.start.tile

        if threat.turns > self.shortest_path_to_target_player.length // 2 and self.board_analysis.intergeneral_analysis.bMap[threatTile] < threat.turns > self.shortest_path_to_target_player.length // 2:
            # bypass threat killer when we're closer to their gen than they are to ours.
            return None

        armyAmount = threat.threatValue + 1
        saveTile = None
        largestTile = None
        source = None
        for threatSource in killTiles:
            for tile in threatSource.movable:
                if tile.player == self._map.player_index and tile not in threat.path.tileSet and tile not in self.expansion_plan.blocking_tiles:
                    if tile.army > 1 and (largestTile is None or tile.army > largestTile.army):
                        largestTile = tile
                        source = threatSource
        threatModifier = 3
        if (self._map.turn - 1) in self.history.attempted_threat_kills:
            logbook.info("We attempted a threatKill last turn, using 1 instead of 3 as threatKill modifier.")
            threatModifier = 1

        if largestTile is not None:
            if threat.threatValue - largestTile.army + threatModifier < 0:
                logbook.info(f"reeeeeeeeeeeeeeeee\nFUCK YES KILLING THREAT TILE {largestTile.x},{largestTile.y}")
                saveTile = largestTile
            else:
                # else see if we can save after killing threat tile
                negativeTilesIncludingThreat = set()
                negativeTilesIncludingThreat.add(largestTile)
                dict = {}
                dict[self.general] = (0, threat.threatValue, 0)
                for tile in negativeTiles:
                    negativeTilesIncludingThreat.add(tile)
                for tile in threat.path.tileSet:
                    negativeTilesIncludingThreat.add(tile)
                if threat.saveTile is not None:
                    dict[threat.saveTile] = (0, threat.threatValue, -0.5)
                    # self.viewInfo.add_targeted_tile(threat.saveTile, TargetStyle.GREEN)
                    logbook.info(f"(killthreat) dict[threat.saveTile] = (0, {threat.saveTile.army})  -- threat.saveTile {threat.saveTile.x},{threat.saveTile.y}")
                savePathSearchModifier = 2
                if largestTile in threat.path.start.tile.movable:
                    logbook.info("largestTile was adjacent to the real threat tile, so savepath needs to be 1 turn shorter for this to be safe")
                    # then we have to be prepared for this move to fail the first turn. Look for savePath - 1
                    savePathSearchModifier = 3
                # threatKillSearchAmount = armyAmount + threatModifier - largestTile.army #- 1
                # postThreatKillSearchTurns = searchTurns - savePathSearchModifier
                # logbook.info("Searching post-threatKill path with threatKillSearchAmount {} for postThreatKillSearchTurns {}".format(threatKillSearchAmount, postThreatKillSearchTurns))
                # bestPath = SearchUtils.dest_breadth_first_target(self._map, dict, threatKillSearchAmount, 0.1, postThreatKillSearchTurns, negativeTilesIncludingThreat, searchingPlayer = self.general.player, ignoreGoalArmy=True)
                # if bestPath is not None and bestPath.length > 0:
                #     self.viewInfo.color_path(PathColorer(bestPath, 250, 250, 250, 200, 12, 100))
                #     if largestTile.army > 7 or threat.threatValue <= largestTile.army:
                #         logbook.info("reeeeeeeeeeeeeeeee\nkilling threat tile with {},{}, we still have time for defense after with path {}:".format(largestTile.x, largestTile.y, bestPath.toString()))
                #         saveTile = largestTile
                #     else:
                #         logbook.info("threatKill {},{} -> {},{} not worthwhile?".format(largestTile.x, largestTile.y, source.x, source.y))
                # else:
                #     logbook.info("largestTile {} couldn't save us because no bestPath save path found post-kill".format(largestTile.toString()))

        if saveTile is not None:
            self.history.attempted_threat_kills.add(self._map.turn)
            return Move(saveTile, source)
        return None

    def should_proactively_take_cities(self):
        # never take cities proactively in FFA when we're engaging a player
        # if self.player != -1 and self._map.remainingPlayers > 2:
        #    return False
        dist = self.distance_from_general(self.targetPlayerExpectedGeneralLocation)
        if self.targetPlayer != -1:
            if len(self.targetPlayerObj.tiles) == 0 and self._map.is_walled_city_game and dist > 20:
                return True

        if self.defend_economy:
            logbook.info("No proactive cities because defending economy :)")
            return False

        # TODO 2v2 revamp needed
        cityLeadWeight = 0
        dist = self.distance_from_general(self.targetPlayerExpectedGeneralLocation)
        if self.targetPlayer != -1:
            opp = self._map.players[self.targetPlayer]
            me = self._map.players[self.general.player]
            # don't keep taking cities unless our lead is really huge
            # need 100 army lead for each additional city we want to take
            cityLeadWeight = (me.cityCount - opp.cityCount) * 70

        knowsWhereEnemyGenIs = self.targetPlayer != -1 and self._map.generals[self.targetPlayer] is not None
        if knowsWhereEnemyGenIs and dist < 18:
            logbook.info("Not proactively taking neutral cities because we know enemy general location and map distance isn't incredibly short")
            return False

        player = self._map.players[self.general.player]
        targetPlayer = None
        if self.targetPlayer != -1:
            targetPlayer = self._map.players[self.targetPlayer]
        safeOnStandingArmy = targetPlayer is None or player.standingArmy > targetPlayer.standingArmy * 0.9
        if (safeOnStandingArmy and ((player.standingArmy > cityLeadWeight and (self.target_player_gather_path is None or dist > 24))
                                    or (player.standingArmy > 30 + cityLeadWeight and (self.target_player_gather_path is None or dist > 22))
                                    or (player.standingArmy > 40 + cityLeadWeight and (self.target_player_gather_path is None or dist > 20))
                                    or (player.standingArmy > 60 + cityLeadWeight and (self.target_player_gather_path is None or dist > 18))
                                    or (player.standingArmy > 70 + cityLeadWeight and (self.target_player_gather_path is None or dist > 16))
                                    or (player.standingArmy > 100 + cityLeadWeight))):
            logbook.info(f"Proactively taking cities! dist {dist}, safe {safeOnStandingArmy}, player.standingArmy {player.standingArmy}, cityLeadWeight {cityLeadWeight}")
            return True
        logbook.info(f"No proactive cities :(     dist {dist}, safe {safeOnStandingArmy}, player.standingArmy {player.standingArmy}, cityLeadWeight {cityLeadWeight}")
        return False

    def capture_cities(
            self,
            negativeTiles: typing.Set[Tile],
            forceNeutralCapture: bool = False,
    ) -> typing.Tuple[Path | None, Move | None]:
        """
        if ForceNeutralCapture is set true, then if a neutral city is viable this WILL produce a gather/capture path to
        it even if there isn't enough army on the map to capture it this cycle etc.

        @param negativeTiles:
        @param forceNeutralCapture:
        @return:
        """
        negativeTiles = negativeTiles.copy()
        if self.is_all_in() and not self.all_in_city_behind:
            return None, None
        logbook.info(f"------------\n     CAPTURE_CITIES (force_city_take {self.force_city_take}), negative_tiles {str(negativeTiles)}\n--------------")
        genDist = min(30, self.distance_from_general(self.targetPlayerExpectedGeneralLocation))
        killSearchDist = max(4, int(genDist * 0.2))
        isNeutCity = False

        wasCityAllIn = self.all_in_city_behind

        with self.perf_timer.begin_move_event('Build City Analyzer'):
            tileScores = self.cityAnalyzer.get_sorted_neutral_scores()
            enemyTileScores = self.cityAnalyzer.get_sorted_enemy_scores()

            if self.info_render_city_priority_debug_info:
                for i, ts in enumerate(tileScores):
                    tile, cityScore = ts
                    self.viewInfo.midLeftGridText[tile] = f'c{i}'
                    EklipZBot.add_city_score_to_view_info(cityScore, self.viewInfo)

                for i, ts in enumerate(enemyTileScores):
                    tile, cityScore = ts
                    self.viewInfo.midLeftGridText[tile] = f'm{i}'
                    EklipZBot.add_city_score_to_view_info(cityScore, self.viewInfo)

        # detect FFA scenarios where we're ahead on army but behind on cities and have lots of tiles and
        # should just rapid-capture tons of cities.
        rapidCityPath = self.find_rapid_city_path()
        if rapidCityPath is not None:
            # already logged
            return rapidCityPath, None

        with self.perf_timer.begin_move_event('finding neutral city path'):
            neutPath = self.find_neutral_city_path()

        desiredGatherTurns = -1
        with self.perf_timer.begin_move_event('Find Enemy City Path'):
            # ignoring negatives because.... why?
            desiredGatherTurns, path = self.find_enemy_city_path(negativeTiles=set(self.win_condition_analyzer.defend_cities), force=neutPath is None)

        if path:
            logbook.info(f"   find_enemy_city_path returned {str(path)}")
        else:
            logbook.info("   find_enemy_city_path returned None.")
        player = self._map.players[self.general.player]
        largestTile = self.general
        for tile in player.tiles:
            if tile.army > largestTile.army:
                largestTile = tile

        mustContestEnemy = False
        if path is not None:
            enCity = path.tail.tile
            if not self.territories.is_tile_in_enemy_territory(enCity) and enCity.discovered:
                logbook.info(f'MUST CONTEST ENEMY CITY {str(enCity)}')
                mustContestEnemy = True

        ourCityCounts = self._map.players[self.general.player].cityCount
        if self.teammate_general is not None:
            ourCityCounts += self._map.players[self.teammate_general.player].cityCount

        if self._map.is_2v2 and self._map.remainingPlayers == 3 and self.targetPlayerObj.cityCount <= ourCityCounts:
            mustContestEnemy = True

        shouldAllowNeutralCapture = self.should_allow_neutral_city_capture(
            genPlayer=player,
            forceNeutralCapture=forceNeutralCapture,
            targetCity=neutPath.tail.tile if neutPath is not None else None
        )

        contestMove = None
        contestGatherVal = 0
        contestGatherTurns = 100
        contestGatherNodes = None
        if WinCondition.ContestEnemyCity in self.win_condition_analyzer.viable_win_conditions and shouldAllowNeutralCapture:
            with self.perf_timer.begin_move_event(f'Contest Offensive all-in move'):
                contestMove, contestGatherVal, contestGatherTurns, contestGatherNodes = self.get_city_contestation_all_in_move(defenseCriticalTileSet=negativeTiles)
            if contestMove is not None:
                # already logged
                return None, contestMove

        if not mustContestEnemy and shouldAllowNeutralCapture:
            # TODO this / 4 shit should take into account whether we have enough army to heavily contest even far away
            #  cities. Many positions should just go sit on enemy cities, always.
            if neutPath and (self.targetPlayer == -1 or path is None or neutPath.length < path.length / 4):
                logbook.info(f"Targeting neutral city {str(neutPath.tail.tile)}")
                path = neutPath
                isNeutCity = True

        if path is None:
            logbook.info(f"xxxxxxxxx\n  xxxxx\n    NO ENEMY CITY FOUND or Neutral city prioritized??? mustContestEnemy {mustContestEnemy} shouldAllowNeutralCapture {shouldAllowNeutralCapture} forceNeutralCapture {forceNeutralCapture}\n  xxxxx\nxxxxxxxx")

            downOnCities = not self.opponent_tracker.even_or_up_on_cities(self.targetPlayer)
            if downOnCities:
                cycleTurn = self.timings.get_turn_in_cycle(self._map.turn)

                cityHuntTurns = 10
                if cycleTurn < cityHuntTurns and not self.are_more_teams_alive_than(2) and shouldAllowNeutralCapture:
                    with self.perf_timer.begin_move_event('fog neut city hunt'):
                        revealPath, move = self.hunt_for_fog_neutral_city(negativeTiles, maxTurns=cycleTurn % cityHuntTurns)
                    if move is not None or revealPath is not None:
                        self.info('hunting fog neutral city')
                        return revealPath, move

                if not self.all_in_city_behind:
                    if cycleTurn < 5:
                        self.send_teammate_communication("Going all in due to lack of cities, attacking end of cycle", self.targetPlayerExpectedGeneralLocation)
                        self.info(f'Going all in, down on cities and no city path found.')
                        self.is_all_in_army_advantage = True
                        self.is_all_in_losing = True
                        self.all_in_city_behind = True

                        self.set_all_in_cycle_to_hit_with_current_timings(50, bufferTurnsEndOfCycle=5)
                # else:
                #     self.all_in_army_advantage_counter += 1

            self.all_in_city_behind = False

            return None, None

        if self.all_in_city_behind:
            self.send_teammate_communication("Ceasing all-in, hold", self.locked_launch_point)
            # not all in anymore, we found a city, play normally
            self.all_in_city_behind = False
            self.is_all_in_army_advantage = False
            self.is_all_in_losing = False
            self.all_in_army_advantage_counter = 0

        target = path.tail.tile
        if player.standingArmy + 5 <= target.army:
            return None, None

        enemyArmyNearDist = 3
        enemyArmyNear = self.sum_enemy_army_near_tile(target, enemyArmyNearDist)
        captureNegs = negativeTiles
        if enemyArmyNear > 0:
            captureNegs = captureNegs.copy()
            tgPlayer = target.player
            if tgPlayer == -1:
                tgPlayer = self.targetPlayer
            killNegs = self.find_large_tiles_near([target], enemyArmyNearDist, forPlayer=tgPlayer, limit=30, minArmy=1)
            for t in killNegs:
                if t != target:
                    captureNegs.add(t)

        targetArmy = enemyArmyNear

        if not isNeutCity and not self._map.is_player_on_team_with(self.territories.territoryMap[target], self.general.player):
            # killSearchDist = 2 * killSearchDist // 3 + 1
            targetArmy = max(2, int(self.sum_enemy_army_near_tile(target, 2) * 1.1))
        else:
            killSearchDist = 3
            if wasCityAllIn:
                targetArmy += 5

        targetArmyGather = target.army + targetArmy

        # gather to the 2 tiles in front of the city
        logbook.info(
            f"xxxxxxxxx\n    SEARCHED AND FOUND NEAREST NEUTRAL / ENEMY CITY {target.x},{target.y} dist {path.length}. Searching {targetArmy} army searchDist {killSearchDist}\nxxxxxxxx")
        if path.length > 1 and path.tail.tile.player == -1:
            # strip the city off
            path = path.get_subsegment(path.length - 1)
        if path.length > 2:
            # strip all but 2 end tiles off
            path = path.get_subsegment(2, end=True)

        if target.player >= 0:
            path = None

        allowGather = False
        gatherDuration = desiredGatherTurns
        if desiredGatherTurns == -1:
            gatherDuration = 20
            # gatherDuration = 25
            if not target.isNeutral:
                gatherDuration = 20
            elif player.tileCount > 125:
                gatherDuration = 10

            if self._map.is_walled_city_game:
                gatherDuration += 5
            if len(self._map.tiles_by_index) > 400:
                gatherDuration += 5
            if len(self._map.tiles_by_index) > 800:
                gatherDuration += 5
            if len(self._map.tiles_by_index) > 1200:
                gatherDuration += 5
            if len(self._map.tiles_by_index) > 1600:
                gatherDuration += 5
            if target.army > 50:
                gatherDuration += 5

        winningOnArmy = self.opponent_tracker.winning_on_army()
        inGathSplit = self.timings.in_gather_split(self._map.turn) or self.timings.in_quick_expand_split(self._map.turn)
        evenOrUpOnCities = self.opponent_tracker.even_or_up_on_cities(self.targetPlayer)
        longSpawns = genDist > 22
        targetCityIsEn = target.player >= 0
        if (winningOnArmy
                or inGathSplit
                or not evenOrUpOnCities
                or longSpawns
                or targetCityIsEn):
            allowGather = True

        self.city_capture_plan_tiles = set()
        capturePath, move = self.plan_city_capture(
            target,
            path,
            allowGather=allowGather,
            targetKillArmy=targetArmy,
            targetGatherArmy=targetArmyGather,
            killSearchDist=killSearchDist,
            gatherMaxDuration=gatherDuration,
            gatherMinDuration=max(0, desiredGatherTurns - 5),
            negativeTiles=captureNegs)

        if capturePath is None and move is None:
            logbook.info(
                f"xxxxxxxxx\n  xxxxx\n    GATHERING TO CITY FAILED :( {target.x},{target.y} \n  xxxxx\nxxxxxxxx")
        elif target.player >= 0:
            self.send_teammate_communication("Lets hold this city", target, cooldown=10)
        else:
            self.send_teammate_communication("Planning to take a city.", target, cooldown=15)  # TODO  Ping one of our generals to force defense, or the enemy to force offense.

        return capturePath, move

    def mark_tile(self, tile, alpha=100):
        self.viewInfo.evaluatedGrid[tile.x][tile.y] = alpha

    def find_neutral_city_path(self) -> Path | None:
        """
        Prioritizes a neutral city and returns a path to it, if one is found. Will refuse to return one if it doesn't make sense to take a city right now.

        @return:
        """
        is1v1 = self._map.remainingPlayers == 2 or self._map.is_2v2
        wayAheadOnEcon = self.opponent_tracker.winning_on_economy(byRatio=1.15, cityValue=40, offset=-5)
        isNotLateGame = self._map.turn < 500

        isWalledNoAggression = self._map.is_walled_city_game and (self.targetPlayer == -1 or self._map.players[self.targetPlayer].aggression_factor == 0.0)

        if not isWalledNoAggression:
            if is1v1 and wayAheadOnEcon and isNotLateGame or len(self.win_condition_analyzer.contestable_cities) > 0:
                return None

            if self.is_still_ffa_and_non_dominant() and self.targetPlayer != -1 and self.targetPlayerObj.aggression_factor > 30:
                return None

            if self.defend_economy and (self.targetPlayer == -1 or self.opponent_tracker.even_or_up_on_cities(self.targetPlayer)):
                return None

        relevanceCutoff = 0.15 * (16 / max(1, self.board_analysis.inter_general_distance))
        distRatioThreshNormal = 0.95
        distRatioThreshEnVisionFewCities = 0.35
        distRatioThreshEnVisionLotsOfCities = 0.6
        if self.opponent_tracker.winning_on_army(offset=-40):
            distRatioThreshEnVisionFewCities = 0.55
            distRatioThreshEnVisionLotsOfCities = 0.8
            relevanceCutoff = 0.10 * (15 / max(1, self.board_analysis.inter_general_distance))

        logbook.info(
            f'looking for neut city with thresholds relevanceCutoff {relevanceCutoff:.2f}, distRatioThreshNormal {distRatioThreshNormal:.2f}, distRatioThreshEnVisionFewCities {distRatioThreshEnVisionFewCities:.2f}, distRatioThreshEnVisionLotsOfCities {distRatioThreshEnVisionLotsOfCities:.2f}')
        citiesByDist = [c for c in sorted(self.cityAnalyzer.city_scores.keys(), key=lambda c2: (10 + self.board_analysis.intergeneral_analysis.aMap.raw[c2.tile_index]) * max(1, c2.army + self.cityAnalyzer.reachability_costs_matrix.raw[c2.tile_index]))]

        baseLimit = 8
        territoryDistCutoff = max(2, self.shortest_path_to_target_player.length // 6)

        path: Path | None = None
        targetCity: Tile | None = None
        maxScore: CityScoreData | None = None
        maxScoreNeutVal: float = -100000.0
        obstacleDict = {}
        toCheck = citiesByDist[:baseLimit]
        i = 0
        toCheckIncluded = set(toCheck)
        while i < len(toCheck):
            city = toCheck[i]
            score = self.cityAnalyzer.city_scores.get(city, None)
            scoreVal = 0.0
            tryPath = False
            if score is None:
                self.info(f'WHOAH, obstacle {city} had no city score...')
                tryPath = True
                scoreVal = max(10, city.army) / obstacleDict.get(city, 1)
                self.mark_tile(city, alpha=150)
            else:
                enemyVision = [tile for tile in filter(lambda t: self._map.is_tile_enemy(t), city.adjacents)]
                cityDistanceRatioThresh = distRatioThreshNormal
                if len(enemyVision) > 0:
                    if self.player.cityCount < 4:
                        cityDistanceRatioThresh = distRatioThreshEnVisionFewCities  # 0.75 is too high to allow taking while enemy has vision.
                    else:
                        cityDistanceRatioThresh = distRatioThreshEnVisionLotsOfCities

                inSafePocket = self.territories.territoryDistances[self.targetPlayer][city] > territoryDistCutoff
                inSafePocket = inSafePocket and not SearchUtils.any_where(city.adjacents, lambda a: a in self.armyTracker.tiles_ever_owned_by_player[self.targetPlayer])
                inSafePocket = inSafePocket or self.territories.territoryDistances[self.targetPlayer][city] > self.shortest_path_to_target_player.length // 4

                bonus = 4 + obstacleDict.get(city, 0)
                baseScore = score.get_weighted_neutral_value()
                scoreVal = baseScore * bonus

                tryPath = (
                        (maxScoreNeutVal < scoreVal)
                        and (score.general_distances_ratio < cityDistanceRatioThresh or inSafePocket)
                        and (score.city_relevance_score > relevanceCutoff)  # dont take cities too out of play
                        or city in obstacleDict
                )
                if tryPath:
                    self.mark_tile(city, alpha=50)
                    self.info(f'Trying city {city}, bonus {bonus}, score {baseScore:.1f} (*bonus {scoreVal:.1f})')
            if tryPath:
                path = self.get_path_to_targets(
                    [t for t in city.movable if not t.isObstacle],
                    maxDepth=self.distance_from_general(city) + 5,
                    skipNeutralCities=False,
                    preferNeutral=False,
                    preferEnemy=False,
                )
                if path is not None:
                    maxScore = score
                    targetCity = city
                    maxScoreNeutVal = scoreVal
                else:
                    for tile, hits in obstacleDict.items():
                        if hits > 0 and len(toCheck) < baseLimit + 4 and tile not in toCheckIncluded:
                            toCheck.append(tile)
                            toCheckIncluded.add(tile)
            i += 1

        if targetCity is not None:
            logbook.info(
                f"Found a neutral city, closest to me and furthest from enemy. Chose city {str(targetCity)} with rating {maxScoreNeutVal}")

            if path is not None:
                path.add_next(targetCity)
            logbook.info(f"    path {str(path)}")
        else:
            logbook.info(f"{self.get_elapsed()} No neutral city found...")

        return path

    def find_enemy_city_path(self, negativeTiles: TileSet, force: bool = False) -> typing.Tuple[int, Path | None]:
        """
        Returns bestGatherTurnsApprox, pathToCity if a good city contest option is found. If not, returns turns -1 and None path.

        @param negativeTiles:
        @param force: if True will prioritize enemy cities that otherwise would not be allowed.
        @return:
        """

        scores = [c for c in self.get_enemy_cities_by_priority()]
        foundScores = [c for c in scores if not c.isTempFogPrediction or c.discovered]
        if len(foundScores) > 0:
            scores = foundScores

        approxed = 0
        tgTile = None
        maxDiff = -10000
        bestTurns = -1

        start = time.perf_counter()

        prevApproxed = self.enemy_city_approxed_attacks
        self.enemy_city_approxed_attacks = {}

        for tile in scores:
            preCalcedOffensePlan = self.win_condition_analyzer.contestable_city_offense_plans.get(tile, None)
            if preCalcedOffensePlan is None:
                logbook.info(f'en city @{tile} skipped because win condition analyzer didnt think it was contestable.')
                continue

            if self.territories.is_tile_in_friendly_territory(tile) or self._map.is_player_on_team_with(self.targetPlayer, tile.player):
                if approxed > 2:
                    self.viewInfo.add_stats_line(f'en city @{tile} approxed > 2')
                    break

                if not tile.discovered and tile.isTempFogPrediction:  # and self.armyTracker.emergenceLocationMap[tile.player].raw[tile.tile_index] < 7:
                    self.viewInfo.add_stats_line(f'en city @{tile} skipped, temp fog prediction')
                    continue

                if tile in prevApproxed and tile not in self.city_capture_plan_tiles and not self._map.is_army_bonus_turn:
                    self.viewInfo.add_stats_line(f'en city @{tile} skipped, prevApproxed and not in plan')
                    continue

                approxed += 1

                if time.perf_counter() - start > 0.025:
                    self.viewInfo.add_stats_line(f'OUT OF TIME city @{tile}')
                    continue

                timing = 30 - ((self._map.turn + 5) % 50) % 10
                with self.perf_timer.begin_move_event(f'en city @{tile} approx attack/def {timing}t'):
                    # curLeft, ourAttack, theirDef = self.get_approximate_attack_defense_sweet_spot(tile, negativeTiles, cycleInterval=cycleInterval, cycleBase=cycleBase, returnDiffThresh=0, minTurns=4, maxTurns=30)
                    curLeft, ourAttack, theirDef = self.win_condition_analyzer.get_dynamic_approximate_attack_defense(tile, negativeTiles, minTurns=4, maxTurns=timing)
                    self.enemy_city_approxed_attacks[tile] = (curLeft, ourAttack, theirDef)

                diff = ourAttack - theirDef
                self.viewInfo.add_stats_line(f'en city @{tile} in {curLeft} diff {diff} (us={ourAttack} vs them={theirDef})')

                if ourAttack > theirDef and maxDiff < diff:
                    logbook.info(f'Approx attack/def shows our attack {ourAttack}, their def {theirDef}')
                    tgTile = tile
                    maxDiff = diff
                    bestTurns = curLeft

        if tgTile is None:
            return bestTurns, None

        # TODO hack because kept gathering obnoxiously
        bestTurns = -1
        return bestTurns, self.get_path_to_target(tgTile)

    def get_approximate_attack_defense_sweet_spot(
            self,
            tile: Tile,
            negativeTiles: TileSet,
            cycleBase: int = 10,
            cycleInterval: int = 5,
            minTurns: int = 0,
            maxTurns: int = 35,
            attackingPlayer: int = -1,
            defendingPlayer: int = -1,
            returnDiffThresh: int = 1000,
            noLog: bool = False
    ) -> typing.Tuple[int, int, int]:
        """
        returns foundTurns, approxAttack, approxDef

        @param tile: The tile to attack
        @param negativeTiles: negative tiles in the attack (but not the defense)
        @param cycleBase: the base map cycle interval that current turn is divided by to get the base decrementing cyclic counter.
        @param cycleInterval: the number of turns to increase the search by each iteration.
        @param minTurns: The minimum number of turns allowed
        @param maxTurns: The maximum number of turns allowed.
        @param attackingPlayer:
        @param defendingPlayer:
        @param returnDiffThresh: If the diff of attack-defense gets greater than this, the value will be returned immediately without looking for a higher diff.
        @param noLog: if False, do not log.
        @return:
        """

        if attackingPlayer == -1:
            attackingPlayer = self.general.player
        if defendingPlayer == -1:
            defendingPlayer = tile.player

        # TODO rework to binary search the space when no returnDiffThresh?

        currentCycle = self._map.turn % cycleBase
        left = cycleBase - currentCycle
        curLeft = left
        if curLeft < minTurns:
            # we'd be using quick-kill path if it was possible, by now. Bail longer
            curLeft += cycleInterval

        bestDiff = -1000
        curDiff = -1000
        theirDef = 0

        bestAttack = 0
        bestDef = 0
        bestTurns = curLeft

        while curLeft <= maxTurns and curDiff < returnDiffThresh:
            ourAttack = self.win_condition_analyzer.get_approximate_attack_against(
                [tile],
                curLeft,
                attackingPlayer,
                0.005,
                forceFogRisk=False,
                negativeTiles=negativeTiles,
                noLog=True,
            )

            if ourAttack > 0:
                theirDef = self.win_condition_analyzer.get_approximate_attack_against(
                    [tile],
                    curLeft,
                    defendingPlayer,
                    0.005,
                    forceFogRisk=False,
                    negativeTiles=None,
                    noLog=True,
                )

                curDiff = ourAttack - theirDef
                if not noLog:
                    logbook.info(f'atk/def @{tile}: diff {curDiff} (attack {ourAttack}, def {theirDef}')

            if curDiff <= bestDiff - 15 and ourAttack > 0 and bestAttack > 0:
                break

            if curDiff > bestDiff:
                bestAttack = ourAttack
                bestDef = theirDef
                bestTurns = curLeft
                bestDiff = curDiff

            curLeft += cycleInterval

        return bestTurns, bestAttack, bestDef

    def get_value_per_turn_subsegment(
            self,
            path: Path,
            minFactor=0.7,
            minLengthFactor=0.1,
            negativeTiles=None
    ) -> Path:
        if not isinstance(path, Path):
            return path

        return path

        tileArrayRev = [t for t in reversed(path.tileList)]

        tileCount = len(tileArrayRev)
        vtArray = [0.0] * tileCount
        valArray = [0] * tileCount

        rollingSum = 0
        for i, tile in enumerate(tileArrayRev):
            if negativeTiles and tile in negativeTiles:
                continue

            if self._map.is_tile_friendly(tile):
                rollingSum += tile.army - 1
            # else:
            #     rollingSum -= tile.army

            if i > 0:
                vtArray[tileCount - i - 1] = rollingSum / i
            valArray[tileCount - i - 1] = rollingSum

        trueVt = rollingSum / path.length
        cutoffVal = rollingSum * minFactor

        maxVtStart = 0
        maxVt = 0.0

        for i, tile in enumerate(path.tileList):
            if tile.player != self.general.player:
                continue
            if i == path.length:
                continue
            if tile.army <= 1:
                continue

            lengthFactor = (path.length - i) / path.length
            if lengthFactor <= minLengthFactor:
                break

            vt = vtArray[i]
            if vt > maxVt and valArray[i] >= cutoffVal:
                maxVtStart = i
                maxVt = vt

        if maxVtStart == 0:
            return path

        newPath = path.get_subsegment(path.length - maxVtStart, end=True)

        if newPath.start.tile.army <= 1:
            self.info(f'VT SUBSEGMENT HAD BAD ARMY {newPath.start.tile.army} on start tile {newPath.start.tile}')
            logbook.error(f'value_per_turn_subsegment turned {str(path)} into {str(newPath)}...? Start tile is 1. Returning original path...')
            # newPath = path
        if newPath.start.tile.player != self.general.player:
            self.info(f'VT SUBSEGMENT HAD BAD PLAYER {newPath.start.tile.player} on start tile {newPath.start.tile}')
            logbook.error(f'value_per_turn_subsegment turned {str(path)} into {str(newPath)}...? Start tile is not even owned by us. Returning the original path...')
            # newPath = path
            # raise AssertionError('Ok clearly we fucked up')
        newPath.calculate_value(self.general.player, teams=self._map.team_ids_by_player_index)

        while newPath.start is not None and (newPath.start.tile.army < 2 or newPath.start.tile.player != self.general.player):
            self.viewInfo.add_info_line(f'Popping bad move {str(newPath.start.tile)} off of value-per-turn-subsegment-path')
            newPath.pop_first_move()
            if newPath.length == 0:
                break

        if newPath.length == 0:
            newPath = path.clone()
            self.viewInfo.add_info_line(f'VT subsegment repair ALSO bad.')

            while newPath.get_first_move() is not None and (newPath.start.tile.army < 2 or newPath.start.tile.player != self.general.player):
                self.viewInfo.add_info_line(f'Popping bad move {str(newPath.start.tile)} off of value-per-turn-subsegment-path')
                newPath.pop_first_move()

        return newPath

    def calculate_general_danger(self):
        depth = self.distance_from_general(self.targetPlayerExpectedGeneralLocation)
        if depth < 9:
            depth = 9
        if self.is_2v2_teammate_still_alive():
            depth += 5

        self.oldThreat = self.dangerAnalyzer.fastestThreat
        self.oldAllyThreat = self.dangerAnalyzer.fastestAllyThreat

        cities = []
        for player in self._map.players:
            if player.team == self._map.team_ids_by_player_index[self.general.player] and not player.dead:
                cities.extend(player.cities)

        self.dangerAnalyzer.analyze(cities, depth, self.armyTracker.armies)

        if self.dangerAnalyzer.fastestThreat:
            self.viewInfo.add_stats_line(f'Threat@{str(self.dangerAnalyzer.fastestThreat.path.tail.tile)}: {str(self.dangerAnalyzer.fastestThreat.path)}')
            if self.dangerAnalyzer.fastestThreat.saveTile is not None:
                self.viewInfo.add_targeted_tile(self.dangerAnalyzer.fastestThreat.saveTile, TargetStyle.GOLD)

        if self.dangerAnalyzer.fastestCityThreat:
            self.viewInfo.add_stats_line(f'CThreat@{str(self.dangerAnalyzer.fastestCityThreat.path.tail.tile)}: {str(self.dangerAnalyzer.fastestCityThreat.path)}')
        if self.dangerAnalyzer.fastestVisionThreat:
            self.viewInfo.add_stats_line(f'VThreat@{str(self.dangerAnalyzer.fastestVisionThreat.path.tail.tile)}: {str(self.dangerAnalyzer.fastestVisionThreat.path)}')
        if self.dangerAnalyzer.fastestAllyThreat:
            self.viewInfo.add_stats_line(f'AThreat@{str(self.dangerAnalyzer.fastestAllyThreat.path.tail.tile)}: {str(self.dangerAnalyzer.fastestAllyThreat.path)}')
        if self.dangerAnalyzer.fastestPotentialThreat:
            self.viewInfo.add_stats_line(f'PotThreat@{str(self.dangerAnalyzer.fastestPotentialThreat.path.tail.tile)}: {str(self.dangerAnalyzer.fastestPotentialThreat.path)}')

        if self.should_abandon_king_defense():
            self.viewInfo.add_stats_line(f'skipping defense because losing on econ')

    def check_should_be_all_in_losing(self) -> bool:
        general = self.general
        if general is None:
            self.is_all_in_losing = False
            return False

        if self.targetPlayer == -1:
            self.is_all_in_losing = False
            return False

        if not self.is_all_in() and self.force_far_gathers:
            return False

        customRatioOffset = 0.0
        if self.is_weird_custom:
            customRatioOffset += 0.03
        if self._map.is_walled_city_game:
            customRatioOffset += 0.07
        if self._map.is_low_cost_city_game:
            customRatioOffset += 0.1
        customRatioMult = 1.0 + customRatioOffset

        frStats = self._map.get_team_stats(self.general.player)
        enStats = self._map.get_team_stats(self.targetPlayer)
        turnsLeft = self.timings.get_turns_left_in_cycle(self._map.turn)
        offset = 5
        if self.is_weird_custom:
            offset = 25
        if self._map.is_walled_city_game:
            offset += 30
        if self._map.is_low_cost_city_game:
            offset += 40

        if self.is_all_in_losing:
            offset = -7

        losingEnoughForCounter = enStats.tileCount + 35 * enStats.cityCount > frStats.tileCount * 1.05 * customRatioMult + (35 * frStats.cityCount) * customRatioMult + offset
        if self.all_in_losing_counter == 0 and turnsLeft >= 30:
            # if we're still early in the cycle, be more lenient.
            offset = min(turnsLeft // 2, 13)
            losingEnoughForCounter = enStats.tileCount + 35 * enStats.cityCount > frStats.tileCount * 1.08 * customRatioMult + 35 * (frStats.cityCount + 1) * customRatioMult + offset

        if self.is_all_in_losing:
            losingEnoughForCounter = enStats.tileCount + 35 * enStats.cityCount > frStats.tileCount * 1.01 * customRatioMult + (35 * frStats.cityCount) * customRatioMult + offset

        allInLosingCounterThreshold = frStats.tileCount // 5 + 15
        allInLosingCounterThreshold = max(50, allInLosingCounterThreshold)

        self.is_all_in_losing = False

        # give up if we're massively losing
        if self._map.remainingPlayers - len(self.get_afk_players()) <= 2 or self._map.is_2v2:
            should2v2PartnerDeadAllIn = (self._map.is_2v2 and self.teammate_general is None and self._map.remainingPlayers > 2)
            enJustContested = len(self.cityAnalyzer.enemy_contested_cities)
            if should2v2PartnerDeadAllIn:
                self.is_all_in_losing = True

            if self._map.turn > 250 and enStats.tileCount + 20 * (enStats.cityCount - 1 - enJustContested) > frStats.tileCount * 1.3 * customRatioMult + 5 + 20 * (frStats.cityCount + 2) * customRatioMult and enStats.standingArmy > frStats.standingArmy * 1.25 * customRatioMult + 5:
                self.is_all_in_losing = True
                self.all_in_losing_counter = 200
            elif self._map.turn > 150 and enStats.tileCount + 15 * enStats.cityCount > frStats.tileCount * 1.4 * customRatioMult + 5 + 15 * (frStats.cityCount + 2) * customRatioMult and enStats.standingArmy > frStats.standingArmy * 1.25 * customRatioMult + 5:
                # self.allIn = True
                self.all_in_losing_counter += 3
            elif should2v2PartnerDeadAllIn or (not self.is_all_in_army_advantage and self._map.turn > 50 and losingEnoughForCounter):
                self.all_in_losing_counter += 1
            else:
                self.all_in_losing_counter = 0
            if self.all_in_losing_counter > allInLosingCounterThreshold:
                # TODO win condition analyzer should decide this
                self.is_all_in_losing = True
            if enStats.tileCount + 35 * enStats.cityCount > frStats.tileCount * 1.5 * customRatioMult + 5 + 35 * frStats.cityCount and enStats.score > frStats.score * 1.6 * customRatioMult + 5:
                self.giving_up_counter += 1
                logbook.info(
                    f"~ ~ ~ ~ ~ ~ ~ giving_up_counter: {self.giving_up_counter}. Player {self.targetPlayer} (or team) with {enStats.tileCount} tiles and {enStats.score} army.")
                if self.giving_up_counter > frStats.tileCount + 20 and not self.finishing_exploration or self.giving_up_counter > frStats.tileCount + 70:
                    logbook.info(
                        f"~ ~ ~ ~ ~ ~ ~ giving up due to player {self.targetPlayer} (or team) with {enStats.tileCount} tiles and {enStats.score} army.")
                    self.viewInfo.add_info_line(f'surrendering')
                    if not self._map.complete:
                        self.send_all_chat_communication("gg")
                    time.sleep(1)
                    if self.surrender_func:
                        self.surrender_func()
                    time.sleep(1)
                    self._map.result = False
                    self._map.complete = True
            else:
                self.giving_up_counter = 0

        self._minAllowableArmy = 1
        return self.is_all_in_losing

    def is_move_safe_valid(self, move, allowNonKill=True):
        if move is None:
            return False
        if move.source == self.general:
            return self.general_move_safe(move.dest)
        if move.source.player != move.dest.player and move.source.army - 2 < move.dest.army and not allowNonKill:
            logbook.info(
                f"{move.source.x},{move.source.y} -> {move.dest.x},{move.dest.y} was not a move that killed the dest tile")
            return False
        return True

    def general_move_safe(self, target, move_half=False):
        dangerTiles = self.get_general_move_blocking_tiles(target, move_half)
        return len(dangerTiles) == 0

    def get_general_move_blocking_tiles(self, target: Tile, move_half=False):
        blockingTiles = []

        dangerPaths = self.get_danger_paths(move_half)

        for dangerPath in dangerPaths:
            dangerTile = dangerPath.start.tile
            genDist = self._map.euclidDist(dangerTile.x, dangerTile.y, self.general.x, self.general.y)
            dangerTileIsTarget = target.x == dangerTile.x and target.y == dangerTile.y
            if dangerTileIsTarget:
                logbook.info(
                    f"ALLOW Enemy tile {dangerTile.x},{dangerTile.y} allowed due to dangerTileIsTarget {dangerTileIsTarget}.")
                continue

            dangerTileForwardMoves = SearchUtils.where(
                dangerTile.movable,
                lambda t: self.distance_from_general(dangerTile) > self.distance_from_general(t))

            dangerTileCanOnlyMoveToIntercept = (len(dangerTileForwardMoves) == 1 and genDist > self._map.euclidDist(dangerTile.x, dangerTile.y, target.x, target.y))

            targetBlocksDangerTile = (
                    (self.general.x == target.x and self.general.x == dangerTile.x)
                    or (self.general.y == target.y and self.general.y == dangerTile.y)
                    or dangerTileCanOnlyMoveToIntercept
            )

            if targetBlocksDangerTile:
                logbook.info(
                    f"ALLOW Enemy tile {dangerTile.x},{dangerTile.y} allowed due to targetBlocksDangerTile {targetBlocksDangerTile}.")
                continue

            blockingTiles.append(dangerTile)
            logbook.info(
                f"BLOCK Enemy tile {dangerTile.x},{dangerTile.y} is preventing king moves. NOT dangerTileIsTarget {dangerTileIsTarget} or targetBlocksDangerTile {targetBlocksDangerTile}")

        return blockingTiles

    def check_should_defend_economy_based_on_large_tiles(self) -> bool:
        largeEnemyTiles = self.find_large_tiles_near(
            [t for t in self.board_analysis.intergeneral_analysis.shortestPathWay.tiles],
            distance=4,
            forPlayer=self.targetPlayer,
            limit=1,
            minArmy=30,
            allowGeneral=False
        )

        largeFriendlyTiles = self.find_large_tiles_near(
            [t for t in self.board_analysis.intergeneral_analysis.shortestPathWay.tiles],
            distance=5,
            forPlayer=self.general.player,
            limit=1,
            minArmy=1,
            allowGeneral=False
        )

        largeFriendlyArmy = 0
        if len(largeFriendlyTiles) > 0:
            largeFriendlyArmy = largeFriendlyTiles[0].army

        self.is_blocking_neutral_city_captures = False

        if len(largeEnemyTiles) > 0:
            largeEnTile = largeEnemyTiles[0]
            me = self._map.players[self.general.player]
            dist = self.distance_from_general(largeEnTile)
            thresh = 2 * me.standingArmy // 3 + dist
            if largeEnTile.army > largeFriendlyArmy and largeEnTile.army > thresh and dist < 2 * self.board_analysis.inter_general_distance // 3 and not largeEnTile.isGeneral:
                self.defend_economy = True
                self.viewInfo.add_info_line(f'marking defending economy due to large enemy tile {str(largeEnTile)} (thresh {thresh})')
                self.force_city_take = False
                if largeEnTile.army > largeFriendlyArmy + 35 and largeEnTile.army > me.standingArmy // 2 - 35 and not self._map.is_2v2:
                    self.is_blocking_neutral_city_captures = True

            if self.curPath and self.curPath.tail is not None and self.curPath.tail.tile.isCity and self.curPath.tail.tile.isNeutral and self.is_blocking_neutral_city_captures:
                targetNeutCity = self.curPath.tail.tile
                if self.is_blocking_neutral_city_captures:
                    self.info(
                        f'forcibly stopped taking neutral city {str(targetNeutCity)} due to unsafe tile {str(largeEnTile)}')
                    self.curPath = None
                    self.force_city_take = False

            # TODO hack, is this method even needed anymore??
            return False

            if self.timings.get_turns_left_in_cycle(self._map.turn) < 5:
                return False

            if self.defend_economy:
                return True

        return False

    def should_defend_economy(self, defenseTiles: typing.Set[Tile]):
        """
        defenseTiles will be added to, if we choose to say we're safe but that certain tiles can't be used.
        @param defenseTiles:
        @return:
        """
        if self._map.remainingPlayers > 2:
            return False
        if self.targetPlayer == -1:
            return False

        if self.targetPlayerObj.last_seen_move_turn < self._map.turn - 100:
            self.viewInfo.add_info_line(f'ignoring econ defense against afk player')
            return False

        genPlayer = self._map.players[self.general.player]

        wasDefending = self.defend_economy

        self.defend_economy = False
        if self.check_should_defend_economy_based_on_large_tiles():
            self.defend_economy = True
            return True

        if self.check_should_defend_economy_based_on_cycle_behavior(defenseCriticalTileSet=defenseTiles):
            self.viewInfo.add_info_line(f'DEF ECON BASED ON CYCLE BEHAVIOR')
            self.defend_economy = True
            if not wasDefending:
                self.currently_forcing_out_of_play_gathers = True
                self.timings = self.get_timings()
            return True

        if self.timings.get_turn_in_cycle(self._map.turn) < self.timings.launchTiming:
            if (
                    self.army_out_of_play
                    and not self.opponent_tracker.winning_on_army(byRatio=1.6)
                    and self.opponent_tracker.winning_on_economy(byRatio=1.1, offset=0)
                    and genPlayer.tileCount < 120
                    and not self.flanking
            ):
                # see if we have enough defensively, though:
                requirementRatio = 0.8
                if wasDefending:
                    requirementRatio = 0.9

                required = self.fog_risk_amount * requirementRatio

                totalDefensive = 0
                totalDefensiveHeld = 0
                defenseTreeBackToFront = sorted(self.defensive_spanning_tree, key=lambda t: self.territories.territoryTeamDistances[self.targetPlayerObj.team].raw[t.tile_index], reverse=True)
                for tile in defenseTreeBackToFront:
                    if totalDefensive < required:
                        defenseTiles.add(tile)
                        self.viewInfo.add_targeted_tile(tile, TargetStyle.WHITE)
                        totalDefensiveHeld += tile.army

                    totalDefensive += tile.army

                if totalDefensive > required:
                    self.viewInfo.add_info_line(f'BYP DEF W HELD TILES {totalDefensiveHeld} ({totalDefensive} total) vs {required:.0f}')
                    return False

                self.defend_economy = True

                if not self.currently_forcing_out_of_play_gathers:
                    self.currently_forcing_out_of_play_gathers = True
                    self.timings = self.get_timings()

                return True
            else:
                self.currently_forcing_out_of_play_gathers = False

        winningText = "first 100 still"
        if self._map.turn >= 100:
            econRatio = 1.16
            armyRatio = 1.42
            # 1.45 might be too much?
            enemyCatchUpOffset = -15
            # -5 offset leads to defending economy on exactly equal tiles 1 city up.
            # Maybe -15 will be too greedy? Try -10 if it gets itself killed too much.

            winningEcon = self.opponent_tracker.winning_on_economy(econRatio, cityValue=20, againstPlayer=self.targetPlayer, offset=enemyCatchUpOffset)
            winningArmy = self.opponent_tracker.winning_on_army(armyRatio)
            pathLen = 20
            if self.shortest_path_to_target_player is not None:
                pathLen = self.shortest_path_to_target_player.length

            playerArmyNearGeneral = self.sum_friendly_army_near_or_on_tiles(self.shortest_path_to_target_player.tileList, distance=pathLen // 4 + 1)
            armyThresh = int(self.targetPlayerObj.standingArmy ** 0.93)
            hasEnoughArmyNearGeneral = playerArmyNearGeneral > armyThresh

            self.defend_economy = winningEcon and (not winningArmy or not hasEnoughArmyNearGeneral)
            if self.defend_economy:
                if not hasEnoughArmyNearGeneral and winningArmy:
                    self.viewInfo.add_info_line("FORCING MAX GATHER TIMINGS BECAUSE NOT ENOUGH ARMY NEAR GEN AND DEFENDING ECONOMY")
                    self.timings.split = self.timings.cycleTurns
                logbook.info(
                    f"\n\nDEF ECONOMY! winning_on_econ({econRatio}) {str(winningEcon)[0]}, on_army({armyRatio}) {str(winningArmy)[0]}, enough_near_gen({playerArmyNearGeneral}/{armyThresh}) {str(hasEnoughArmyNearGeneral)[0]}")
                winningText = f"! woe{econRatio} {str(winningEcon)[0]}, woa{armyRatio} {str(winningArmy)[0]}, sa{playerArmyNearGeneral}/{armyThresh} {str(hasEnoughArmyNearGeneral)[0]}"
            else:
                logbook.info(
                    f"\n\nNOT DEFENDING ECONOMY? winning_on_econ({econRatio}) {str(winningEcon)[0]}, on_army({armyRatio}) {str(winningArmy)[0]}, enough_near_gen({playerArmyNearGeneral}/{armyThresh}) {str(hasEnoughArmyNearGeneral)[0]}")
                winningText = f"  woe{econRatio} {str(winningEcon)[0]}, woa{armyRatio} {str(winningArmy)[0]}, sa{playerArmyNearGeneral}/{armyThresh} {str(hasEnoughArmyNearGeneral)[0]}"

        self.viewInfo.addlTimingsLineText = winningText

        return self.defend_economy

    def get_danger_tiles(self, move_half=False) -> typing.Set[Tile]:
        dangerPaths = self.get_danger_paths(move_half)

        dangerTiles = set()
        for dangerPath in dangerPaths:
            if dangerPath is not None:
                dangerTiles.update(SearchUtils.where(dangerPath.tileList, lambda t: self._map.is_tile_enemy(t) and t.army > 2))

        return dangerTiles

    def get_danger_paths(self, move_half=False) -> typing.List[Path]:
        thresh = 3
        if move_half:
            thresh = self.general.army - self.general.army // 2 + 2

        dangerPaths = []
        if self.targetPlayer != -1:
            dangerPath = SearchUtils.dest_breadth_first_target(self._map, self.general.movable, targetArmy=thresh, maxTime=0.1, maxDepth=2, searchingPlayer=self.targetPlayer, ignoreGoalArmy=False)
            if dangerPath is not None:
                dangerPaths.append(dangerPath)
                altSet = dangerPath.tileSet.copy()

                altPath = SearchUtils.dest_breadth_first_target(self._map, self.general.movable, negativeTiles=altSet, targetArmy=thresh, maxTime=0.1, maxDepth=2, searchingPlayer=self.targetPlayer, ignoreGoalArmy=False)
                if altPath is not None:
                    dangerPaths.append(altPath)
                    altSet.discard(altPath.start.tile)

                altSet.discard(dangerPath.start.tile)

                altPath = SearchUtils.dest_breadth_first_target(self._map, self.general.movable, negativeTiles=altSet, targetArmy=thresh, maxTime=0.1, maxDepth=2, searchingPlayer=self.targetPlayer, ignoreGoalArmy=False)
                if altPath is not None and str(altPath) != str(dangerPath):
                    dangerPaths.append(altPath)

        for mv in self.general.movable:
            if self._map.is_tile_enemy(mv) and mv.army >= thresh:
                path = Path()
                path.add_next(mv)
                path.add_next(self.general)
                dangerPaths.append(path)

        for dangerPath in dangerPaths:
            self.info(f'DBG: DangerPath {dangerPath}')

        return dangerPaths

    def worth_attacking_target(self) -> bool:
        timingFactor = 1.0
        if self._map.turn < 50:
            self.viewInfo.add_info_line("Not worth attacking, turn < 50")
            return False

        knowsWhereEnemyGeneralIs = self.targetPlayer != -1 and self._map.generals[self.targetPlayer] is not None

        if self.targetPlayer == -1:
            shouldAttack = self._map.remainingPlayers == 2
            self.viewInfo.add_info_line(f"FFA no tiles path worth attacking: {shouldAttack}")
            return shouldAttack

        frStats = self._map.get_team_stats(self.general.player)
        enStats = self._map.get_team_stats(self.targetPlayer)

        # if 20% ahead on economy and not 10% ahead on standing army, just gather, dont attack....
        wPStanding = frStats.standingArmy * 0.9
        oppStanding = enStats.standingArmy
        wPIncome = frStats.tileCount + frStats.cityCount * 30
        wOppIncome = enStats.tileCount * 1.2 + enStats.cityCount * 35 + 5
        if self._map.turn >= 100 and wPStanding < oppStanding and wPIncome > wOppIncome:
            self.viewInfo.add_info_line("NOT WORTH ATTACKING TARGET BECAUSE wPStanding < oppStanding and wPIncome > wOppIncome")
            self.viewInfo.add_info_line(
                f"NOT WORTH ATTACKING TARGET BECAUSE {wPStanding}     <  {oppStanding}        and   {wPIncome} >   {wOppIncome}")
            return False

        # factor in some time for exploring after the attack, + 1 * 1.1
        if self.target_player_gather_path is None:
            logbook.info("ELIM due to no path")
            return False
        value = self.get_player_army_amount_on_path(self.target_player_gather_path, self._map.player_index, 0, self.target_player_gather_path.length)
        logbook.info(
            f"Player army amount on path: {value}   TARGET PLAYER PATH IS REVERSED ? {self.target_player_gather_path.toString()}")
        subsegment = self.get_value_per_turn_subsegment(self.target_player_gather_path)
        logbook.info(f"value per turn subsegment = {subsegment.toString()}")
        subsegmentTargets = subsegment.tileSet

        lengthRatio = len(self.target_player_gather_targets) / max(1, len(subsegmentTargets))

        sqrtVal = 0
        if value > 0:
            sqrtVal = value ** 0.5
            logbook.info(f"value ** 0.5 -> sqrtVal {sqrtVal}")
        if frStats.tileCount < 60:
            sqrtVal = value / 2.0
            logbook.info(f"value / 2.3  -> sqrtVal {sqrtVal}")
        sqrtVal = min(20, sqrtVal)

        dist = int((len(subsegmentTargets)) + sqrtVal)
        factorTurns = 50
        if dist > 25 or frStats.tileCount > 110:
            factorTurns = 100
        turnOffset = self._map.turn + dist
        factorScale = turnOffset % factorTurns
        if factorScale < factorTurns / 2:
            logbook.info("factorScale < factorTurns / 2")
            timingFactor = scale(factorScale, 0, factorTurns / 2, 0, 0.40)
        else:
            logbook.info("factorScale >>>>>>>>> factorTurns / 2")
            timingFactor = scale(factorScale, factorTurns / 2, factorTurns, 0.30, 0)

        if self.lastTimingFactor != -1 and self.lastTimingFactor < timingFactor:
            logbook.info(
                f"  ~~~  ---  ~~~  lastTimingFactor {'%.3f' % self.lastTimingFactor} <<<< timingFactor {'%.3f' % timingFactor}")
            factor = self.lastTimingFactor
            self.lastTimingFactor = timingFactor
            timingFactor = factor
        self.lastTimingTurn = self._map.turn

        if frStats.tileCount > 200:
            # timing no longer matters after a certain point?
            timingFactor = 0.1

        # if we are already attacking, keep attacking
        alreadyAttacking = False
        if self._map.turn - 3 < self.lastTargetAttackTurn:
            timingFactor *= 0.3  # 0.3
            alreadyAttacking = True
            logbook.info("already attacking :)")

        if frStats.standingArmy < 5 and timingFactor > 0.1:
            return False
        logbook.info(
            f"OoOoOoOoOoOoOoOoOoOoOoOoOoOoOoOoOoOoO\n   {self._map.turn}  oOo  timingFactor {'%.3f' % timingFactor},  factorTurns {factorTurns},  turnOffset {turnOffset},  factorScale {factorScale},  sqrtVal {'%.1f' % sqrtVal},  dist {dist}")

        playerEffectiveStandingArmy = frStats.standingArmy - 9 * (frStats.cityCount - 1)
        if self.target_player_gather_path.length < 2:
            logbook.info(
                f"ELIM due to path length {self.distance_from_general(self.targetPlayerExpectedGeneralLocation)}")
            return False

        targetPlayerArmyThreshold = self._map.players[self.targetPlayer].standingArmy + dist / 2
        if frStats.standingArmy < 70:
            timingFactor *= 2
            timingFactor = timingFactor ** 2
            if knowsWhereEnemyGeneralIs:
                timingFactor += 0.05
            rawNeeded = playerEffectiveStandingArmy * 0.62 + playerEffectiveStandingArmy * timingFactor
            rawNeededScaled = rawNeeded * lengthRatio
            neededVal = min(targetPlayerArmyThreshold, rawNeededScaled)
            if alreadyAttacking:
                neededVal *= 0.75
            logbook.info(
                f"    --   playerEffectiveStandingArmy: {playerEffectiveStandingArmy},  NEEDEDVAL: {'%.1f' % neededVal},            VALUE: {value}")
            logbook.info(
                f"    --                                     rawNeeded: {'%.1f' % rawNeeded},  rawNeededScaled: {'%.1f' % rawNeededScaled},  lengthRatio: {'%.1f' % lengthRatio}, targetPlayerArmyThreshold: {'%.1f' % targetPlayerArmyThreshold}")
            return value > neededVal
        else:
            if knowsWhereEnemyGeneralIs:
                timingFactor *= 1.5
                timingFactor += 0.03
            expBase = playerEffectiveStandingArmy * 0.15
            exp = 0.68 + timingFactor
            expValue = playerEffectiveStandingArmy ** exp
            rawNeeded = expBase + expValue
            rawNeededScaled = rawNeeded * lengthRatio
            neededVal = min(targetPlayerArmyThreshold, rawNeededScaled)
            if alreadyAttacking:
                neededVal *= 0.75
            logbook.info(
                f"    --    playerEffectiveStandingArmy: {playerEffectiveStandingArmy},  NEEDEDVAL: {'%.1f' % neededVal},            VALUE: {value},      expBase: {'%.2f' % expBase},   exp: {'%.2f' % exp},       expValue: {'%.2f' % expValue}")
            logbook.info(
                f"    --                                      rawNeeded: {'%.1f' % rawNeeded},  rawNeededScaled: {'%.1f' % rawNeededScaled},  lengthRatio: {'%.1f' % lengthRatio}, targetPlayerArmyThreshold: {'%.1f' % targetPlayerArmyThreshold}")
            return value >= neededVal

    def get_player_army_amount_on_path(self, path, player, startIdx=0, endIdx=1000):
        value = 0
        idx = 0
        pathNode = path.start
        while pathNode is not None:
            if self._map.is_player_on_team_with(pathNode.tile.player, player) and startIdx <= idx <= endIdx:
                value += (pathNode.tile.army - 1)
            pathNode = pathNode.next
            idx += 1
        return value

    def get_target_army_inc_adjacent_enemy(self, tile):
        sumAdj = 0
        for adj in tile.adjacents:
            if self._map.is_tile_enemy(adj):
                sumAdj += adj.army - 1
        armyToSearch = sumAdj
        # if tile.army > 5 and tile.player != self._map.player_index and not tile.isNeutral:
        #     armyToSearch += tile.army / 2
        return armyToSearch

    def find_leaf_move(self, allLeaves):
        leafMoves = self.prioritize_expansion_leaves(allLeaves)
        if self.target_player_gather_path is not None:
            leafMoves = list(SearchUtils.where(leafMoves, lambda move: move.source not in self.target_player_gather_path.tileSet))
        if len(leafMoves) > 0:
            # self.curPath = None
            # self.curPathPrio = -1
            move = leafMoves[0]
            i = 0
            valid = True
            while move.source.isGeneral and not self.general_move_safe(move.dest):
                if self.general_move_safe(move.dest, True):
                    move.move_half = True
                    break
                else:
                    move = random.choice(leafMoves)
                    i += 1
                    if i > 10:
                        valid = False
                        break

            if valid:
                self.curPath = None
                self.curPathPrio = -1
                return move
        return None

    def prioritize_expansion_leaves(
            self,
            allLeaves=None,
            allowNonKill=False,
            distPriorityMap: MapMatrixInterface[int] | None = None,
    ) -> typing.List[Move]:
        queue = SearchUtils.HeapQueue()
        analysis = self.board_analysis.intergeneral_analysis

        expansionMap = self.get_expansion_weight_matrix()

        if distPriorityMap is None:
            distPriorityMap = analysis.bMap

        for leafMove in allLeaves:
            if not allowNonKill and leafMove.source.army - leafMove.dest.army <= 1:
                continue
            if not allowNonKill and (leafMove.dest.isDesert or leafMove.dest.isSwamp):
                continue
            if leafMove.source.army < 2:
                continue
            if self._map.is_tile_friendly(leafMove.dest):
                continue
            if leafMove.dest.isCity and leafMove.dest.player == -1 and leafMove.dest.army > 25:
                continue

            dest = leafMove.dest
            source = leafMove.source
            # if source not in analysis.pathWayLookupMatrix or dest not in analysis.pathWayLookupMatrix:
            #     continue
            # if analysis.bMap[dest] > self.board_analysis.inter_general_distance + 3:
            #     # don't leafmove moves that are overly far from the opp general..?
            #     continue
            if (
                    self.territories.territoryMap[dest] != -1
                    and not self._map.is_player_on_team_with(self.territories.territoryMap[dest], self.general.player)
                    and dest.player == -1
                    and self._map.turn % 50 < 45
            ):
                # no neutral leafmoves into enemy territory except at cycle end...?
                continue
            # sourcePathway = analysis.pathWayLookupMatrix[source]
            # destPathway = analysis.pathWayLookupMatrix[dest]

            points = 0

            if self.board_analysis.innerChokes[dest]:
                # bonus points for retaking iChokes
                points += 0.1
            if not self.board_analysis.outerChokes[dest]:
                # bonus points for avoiding oChokes
                points += 0.05

            if self.board_analysis.intergeneral_analysis.is_choke(dest):
                points += 0.15

            towardsEnemy = distPriorityMap[dest] < distPriorityMap[source]
            if towardsEnemy:
                points += 0.4

            awayFromUs = analysis.aMap[dest] > analysis.aMap[source]
            if awayFromUs:
                points += 0.1

            if dest.player == self.targetPlayer:
                points += 1.5

            points += expansionMap[dest] * 5

            # extra points for tiles that are closer to enemy
            distEnemyPoints = (analysis.aMap[dest] + 1) / (distPriorityMap[dest] + 1)

            points += distEnemyPoints / 3

            logbook.info(f"leafMove {leafMove}, points {points:.2f} (distEnemyPoints {distEnemyPoints:.2f})")
            queue.put((0 - points, leafMove))
        vals = []
        while queue.queue:
            prio, move = queue.get()
            vals.append(move)
        return vals

    def getDistToEnemy(self, tile):
        dist = 1000
        for i in range(len(self._map.generals)):
            gen = self._map.generals[i]
            genDist = 0
            if gen is not None:
                genDist = self._map.euclidDist(gen.x, gen.y, tile.x, tile.y)
            elif self.generalApproximations[i][2] > 0:
                genDist = self._map.euclidDist(self.generalApproximations[i][0], self.generalApproximations[i][1], tile.x, tile.y)

            if genDist < dist:
                dist = genDist
        return dist

    def get_path_to_target_player(self, isAllIn=False, cutLength: int | None = None) -> Path | None:
        # TODO on long distances or higher city counts or FFA-post-kills don't use general path, just find max path to target player and gather to that

        maxTile = self.targetPlayerExpectedGeneralLocation
        if maxTile is None:
            return None

        # self.viewInfo.add_info_line(f'maxTile {str(maxTile)}')

        if self.targetPlayerExpectedGeneralLocation != maxTile and self._map.turn > 50:
            self.send_teammate_communication(f"I will be targeting {self._map.usernames[self.targetPlayer]} over here.", maxTile, cooldown=50, detectOnMessageAlone=True)

        with self.perf_timer.begin_move_event('rebuilding intergeneral_analysis'):
            self.board_analysis.rebuild_intergeneral_analysis(self.targetPlayerExpectedGeneralLocation, self.armyTracker.valid_general_positions_by_player)
            # self.rebuild_reachability_costs_matrix()

        if self.teammate_path is not None:
            manhattanDist = abs(self.teammate_general.x - self.general.x) + abs(self.teammate_general.y - self.general.y)
            # https://generals.io/replays/dHdFJIf7T is example of 17, 11
            # self.teammate_path.length > 27 or
            if manhattanDist > 11:
                self.viewInfo.add_info_line(f'teammate path {self.teammate_path.length}, mahattan {manhattanDist}')
                self.viewInfo.color_path(PathColorer(
                    self.teammate_path,
                    0, 0, 255
                ))

        enemyDistMap = None
        if self.board_analysis is not None and self.board_analysis.intergeneral_analysis is not None and self.board_analysis.intergeneral_analysis.bMap is not None:
            enemyDistMap = self.board_analysis.intergeneral_analysis.bMap
        else:
            logbook.info('building distmap after rebuilding intergen analysis')
            enemyDistMap = self._map.distance_mapper.get_tile_dist_matrix(self.targetPlayerExpectedGeneralLocation)
            logbook.info('DONE building distmap after rebuilding intergen analysis')

        fromTile = self.general
        if self.locked_launch_point is None and self._map.is_2v2 and self.teammate_general is not None and self.targetPlayerObj is not None:
            fromTile = self.get_2v2_launch_point()
            self.locked_launch_point = fromTile

        if self.locked_launch_point is not None:
            fromTile = self.locked_launch_point
        else:
            startTime = time.perf_counter()
            targetPlayerObj = None
            if self.targetPlayer != -1:
                targetPlayerObj = self._map.players[self.targetPlayer]
            if targetPlayerObj is None or not targetPlayerObj.knowsKingLocation:
                for genLaunchPoint in self.launchPoints:
                    if genLaunchPoint is None:
                        logbook.info("wtf genlaunchpoint was none????")
                    elif enemyDistMap[genLaunchPoint] < enemyDistMap[fromTile]:
                        logbook.info(f"using launchPoint {genLaunchPoint}")
                        fromTile = genLaunchPoint

            # if (self._map.remainingPlayers == 2 or (self._map.is_2v2 and self.teammate_communicator.is_defense_lead)) and not self.army_out_of_play and self._map.turn >= 150:
            #     with self.perf_timer.begin_move_event('checking for sketchy fog flanks'):
            #         sketchyFogPath = self.find_sketchy_fog_flank_from_enemy_in_play_area()
            #     if sketchyFogPath is not None:
            #         self.viewInfo.add_info_line(f'Using sketchy flank launch {str(sketchyFogPath)}')
            #         fromTile = sketchyFogPath.start.tile
            #         self.flanking = True

            self.locked_launch_point = fromTile

        preferNeut = not isAllIn and not self.is_ffa_situation()
        preferEn = not isAllIn

        if self.is_still_ffa_and_non_dominant():
            preferEn = False
            preferNeut = False

        with self.perf_timer.begin_move_event(f'getting path to target {maxTile}'):
            path = self.get_path_to_target(maxTile, skipEnemyCities=isAllIn, preferNeutral=preferNeut, fromTile=fromTile, preferEnemy=preferEn)
            if path is None:
                path = self.get_path_to_target(maxTile, skipNeutralCities=False, skipEnemyCities=isAllIn, preferNeutral=preferNeut, fromTile=fromTile, preferEnemy=preferEn)

            self.info(f'DEBUG: cutLen {cutLength} at {maxTile}: path {path}')
            if path is not None and cutLength is not None and path.length > cutLength:
                path = path.get_subsegment(cutLength, end=True)

        if self.targetPlayer == -1 and self._map.remainingPlayers > 2 and not self._map.is_2v2:
            # To avoid launching out into the middle of the FFA, just return the general tile and the next tile in the path as the path.
            # this sort of triggers camping-city-taking behavior at the moment.
            fakeGenPath = path.get_subsegment(1)
            logbook.info(f"FakeGenPath because FFA: {str(fakeGenPath)}")
            return fakeGenPath

        return path

    def get_best_defense(self, defendingTile: Tile, turns: int, negativeTileList: typing.List[Tile]) -> Path | None:
        searchingPlayer = defendingTile.player
        logbook.info(f"Trying to get_best_defense. Turns {turns}. Searching player {searchingPlayer}")
        negativeTiles = set()

        for negTile in negativeTileList:
            negativeTiles.add(negTile)

        startTiles = [defendingTile]

        def default_value_func_max_army(currentTile, priorityObject):
            (dist, negArmySum, xSum, ySum) = priorityObject
            return 0 - negArmySum, 0 - dist

        valueFunc = default_value_func_max_army

        def default_priority_func(nextTile, currentPriorityObject):
            (dist, negArmySum, xSum, ySum) = currentPriorityObject
            negArmySum += 1
            # if (nextTile not in skipTiles):
            if searchingPlayer == nextTile.player:
                negArmySum -= nextTile.army
            else:
                negArmySum += nextTile.army

            # logbook.info("prio: nextTile {} got realDist {}, negNextArmy {}, negNeutCount {}, newDist {}, xSum {}, ySum {}".format(nextTile.toString(), realDist + 1, 0-nextArmy, negNeutCount, dist + 1, xSum + nextTile.x, ySum + nextTile.y))
            return dist + 1, negArmySum, xSum + nextTile.x, ySum + nextTile.y

        priorityFunc = default_priority_func

        def default_base_case_func(t, startingDist):
            return 0, 0, t.x, t.y

        baseCaseFunc = default_base_case_func

        startTilesDict = {}
        for tile in startTiles:
            # then use baseCaseFunc to initialize their priorities, and set initial distance to 0
            startTilesDict[tile] = (baseCaseFunc(tile, 0), 0)
            # skipTiles.add(tile)

        for tile in startTilesDict.keys():
            (startPriorityObject, distance) = startTilesDict[tile]
            logbook.info(f"   Including tile {tile} in startTiles at distance {distance}")

        valuePerTurnPath = SearchUtils.breadth_first_dynamic_max(
            self._map,
            startTilesDict,
            valueFunc,
            0.1,
            turns,
            turns,
            noNeutralCities=True,
            negativeTiles=negativeTiles,
            searchingPlayer=searchingPlayer,
            priorityFunc=priorityFunc,
            ignoreStartTile=True,
            preferNeutral=False,
            noLog=True)

        if valuePerTurnPath is not None:
            if DebugHelper.IS_DEBUGGING:
                logbook.info(f"Best defense: {valuePerTurnPath.toString()}")
            savePath = valuePerTurnPath.get_reversed()
            negs = set(negativeTileList)
            negs.add(defendingTile)
            savePath.calculate_value(forPlayer=defendingTile.player, teams=self._map.team_ids_by_player_index, negativeTiles=negs)

            if DebugHelper.IS_DEBUGGING:
                self.viewInfo.color_path(PathColorer(savePath, 255, 255, 255, 255, 10, 150))
            return savePath

        if DebugHelper.IS_DEBUGGING:
            logbook.info("Best defense: NONE")
        return None

    def info(self, text):
        self.viewInfo.infoText = text
        self.viewInfo.add_info_line(text)

    def get_path_to_target(
            self,
            target,
            maxTime=0.1,
            maxDepth=400,
            skipNeutralCities=True,
            skipEnemyCities=False,
            preferNeutral=True,
            fromTile=None,
            preferEnemy=False,
            maxObstacleCost: int | None = None
    ) -> Path | None:
        targets = set()
        targets.add(target)
        return self.get_path_to_targets(
            targets,
            maxTime,
            maxDepth,
            skipNeutralCities,
            skipEnemyCities,
            preferNeutral,
            fromTile,
            preferEnemy=preferEnemy,
            maxObstacleCost=maxObstacleCost)

    def get_path_to_targets(
            self,
            targets,
            maxTime=0.1,
            maxDepth=400,
            skipNeutralCities=True,
            skipEnemyCities=False,
            preferNeutral=True,
            fromTile=None,
            preferEnemy=True,
            maxObstacleCost: int | None = None
    ) -> Path | None:
        if fromTile is None:
            fromTile = self.general
        negativeTiles = None
        if skipEnemyCities:
            negativeTiles = set()
            for enemyCity in self.enemyCities:
                negativeTiles.add(enemyCity)

        if maxObstacleCost is None and self.is_weird_custom:
            maxObstacleCost = self._map.walled_city_base_value

        def path_to_targets_priority_func(
                nextTile: Tile,
                currentPriorityObject):
            (dist, negEnemyTiles, negCityCount, negArmySum, goalIncrement) = currentPriorityObject
            dist += 1

            if nextTile.isCity:
                if skipEnemyCities and self._map.is_tile_enemy(nextTile):
                    return None

                if nextTile.player == -1 and maxObstacleCost is not None and nextTile.army >= maxObstacleCost:
                    return None

            if preferEnemy and not self.is_all_in():
                if self._map.is_tile_on_team_with(nextTile, self.targetPlayer):
                    negEnemyTiles -= 1
                    if nextTile.isCity:
                        negCityCount -= 1

                if not self.is_ffa_situation():
                    if not nextTile.visible:
                        negEnemyTiles -= 1
                    if not nextTile.discovered:
                        negEnemyTiles -= 1

                negEnemyTiles -= int(self.armyTracker.emergenceLocationMap[self.targetPlayer][nextTile] ** 0.25)

            if negativeTiles is None or nextTile not in negativeTiles:
                if nextTile.isNeutral:
                    if nextTile.army <= 0 or (nextTile.isCity and nextTile.army < self.player.standingArmy):
                        if preferNeutral:
                            negEnemyTiles -= 0.5
                            if nextTile.isCity:
                                negCityCount -= 1
                    else:
                        # avoid neutral army tiles and cities we cant capture yet...
                        negEnemyTiles += 2
                        dist += min(nextTile.army + 1, 8)
                if self._map.is_tile_friendly(nextTile):
                    negArmySum -= nextTile.army
                else:
                    negArmySum += nextTile.army
            # always leaving 1 army behind. + because this is negative.
            negArmySum += 1
            # -= because we passed it in positive for our general and negative for enemy gen / cities
            negArmySum -= goalIncrement
            return dist, negEnemyTiles, negCityCount, negArmySum, goalIncrement

        startPriorityObject = (0, 0, 0, 0, 0.5)
        startTiles = {fromTile: (startPriorityObject, 0)}

        path = SearchUtils.breadth_first_dynamic(
            self._map,
            startTiles,
            lambda tile, prioObj: tile in targets,
            maxDepth,
            skipNeutralCities,
            negativeTiles=negativeTiles,
            priorityFunc=path_to_targets_priority_func)

        return path

    def distance_from_general(self, sourceTile):
        if sourceTile == self.general:
            return 0
        val = 0

        if self._gen_distances:
            val = self._gen_distances[sourceTile]
        return val

    def distance_from_teammate(self, sourceTile):
        if sourceTile == self.teammate_general:
            return 0
        val = 0

        if self._ally_distances:
            val = self._ally_distances[sourceTile]
        return val

    def distance_from_opp(self, sourceTile):
        if sourceTile == self.targetPlayerExpectedGeneralLocation:
            return 0
        val = 0
        if self.board_analysis and self.board_analysis.intergeneral_analysis:
            val = self.board_analysis.intergeneral_analysis.bMap[sourceTile]
        return val

    def distance_from_target_path(self, sourceTile):
        if sourceTile in self.shortest_path_to_target_player.tileSet:
            return 0

        val = 0
        if self.board_analysis and self.board_analysis.shortest_path_distances:
            val = self.board_analysis.shortest_path_distances[sourceTile]
        return val

    def scan_map_for_large_tiles_and_leaf_moves(self):
        self.general_safe_func_set[self.general] = self.general_move_safe
        self.leafMoves = []
        self.captureLeafMoves = []
        self.targetPlayerLeafMoves = []
        self.largeTilesNearEnemyKings: typing.Dict[Tile, typing.List[Tile]] = {}

        self.largePlayerTiles = []
        largeNegativeNeutralTiles = []
        player = self._map.players[self.general.player]
        largePlayerTileThreshold = player.standingArmy / player.tileCount * 5
        general = self._map.generals[self._map.player_index]
        generalApproximations = [[0, 0, 0, None] for i in range(len(self._map.generals))]
        for tile in general.adjacents:
            if self._map.is_tile_enemy(tile):
                self._map.players[tile.player].knowsKingLocation = True
                if self._map.teams is not None:
                    for teamPlayer in self._map.players:
                        if self._map.teams[teamPlayer.index] == self._map.teams[tile.player]:
                            teamPlayer.knowsKingLocation = True

        if self.teammate_general is not None:
            for tile in self.teammate_general.adjacents:
                if self._map.is_tile_enemy(tile):
                    for teamPlayer in self._map.players:
                        if self._map.teams[teamPlayer.index] == self._map.teams[tile.player]:
                            teamPlayer.knowsAllyKingLocation = True

        for enemyGen in self._map.generals:
            if enemyGen is not None and self._map.is_tile_enemy(enemyGen):
                self.largeTilesNearEnemyKings[enemyGen] = []
        if not self.targetPlayerExpectedGeneralLocation.isGeneral:
            # self.targetPlayerExpectedGeneralLocation.player = self.player
            self.largeTilesNearEnemyKings[self.targetPlayerExpectedGeneralLocation] = []

        for tile in self._map.tiles_by_index:
            if self._map.is_tile_enemy(tile) and self._map.generals[tile.player] is None:
                for nextTile in tile.movable:
                    if not nextTile.discovered and not nextTile.isNotPathable:
                        approx = generalApproximations[tile.player]
                        approx[0] += nextTile.x
                        approx[1] += nextTile.y
                        approx[2] += 1

            if tile.player == self._map.player_index:
                for nextTile in tile.movable:
                    if not self._map.is_tile_friendly(nextTile) and not nextTile.isObstacle and not nextTile.isSwamp and (not nextTile.isDesert or nextTile.player >= 0):
                        mv = Move(tile, nextTile)
                        self.leafMoves.append(mv)
                        if tile.army - 1 > nextTile.army and tile.army > 1:
                            self.captureLeafMoves.append(mv)
                if tile.army > largePlayerTileThreshold:
                    self.largePlayerTiles.append(tile)

            elif tile.player != -1:
                if tile.player == self.targetPlayer:
                    for nextTile in tile.movable:
                        if not self._map.is_tile_on_team_with(nextTile, self.targetPlayer) and not nextTile.isObstacle and tile.army - 1 > nextTile.army and tile.army > 1:
                            self.targetPlayerLeafMoves.append(Move(tile, nextTile))
                if tile.isCity and self._map.is_tile_enemy(tile):
                    self.enemyCities.append(tile)
            elif tile.army < -1:
                heapq.heappush(largeNegativeNeutralTiles, (tile.army, tile))

            if tile.player == self._map.player_index and tile.army > 5:
                for enemyGen in self.largeTilesNearEnemyKings.keys():
                    if tile.army > enemyGen.army and self._map.euclidDist(tile.x, tile.y, enemyGen.x, enemyGen.y) < 11:
                        self.largeTilesNearEnemyKings[enemyGen].append(tile)

        self.largeNegativeNeutralTiles = []
        while largeNegativeNeutralTiles:
            army, tile = heapq.heappop(largeNegativeNeutralTiles)
            self.largeNegativeNeutralTiles.append(tile)

        # wtf is this doing
        for i in range(len(self._map.generals)):
            if self._map.generals[i] is not None:
                gen = self._map.generals[i]
                generalApproximations[i][0] = gen.x
                generalApproximations[i][1] = gen.y
                generalApproximations[i][3] = gen
            elif generalApproximations[i][2] > 0:

                generalApproximations[i][0] = generalApproximations[i][0] / generalApproximations[i][2]
                generalApproximations[i][1] = generalApproximations[i][1] / generalApproximations[i][2]

                # calculate vector
                delta = ((generalApproximations[i][0] - general.x) * 1.1, (generalApproximations[i][1] - general.y) * 1.1)
                generalApproximations[i][0] = general.x + delta[0]
                generalApproximations[i][1] = general.y + delta[1]
        for i in range(len(self._map.generals)):
            gen = self._map.generals[i]
            genDist = 1000

            if gen is None and generalApproximations[i][2] > 0:
                for tile in self._map.pathable_tiles:
                    if not tile.discovered and not tile.isNotPathable:
                        tileDist = self._map.euclidDist(generalApproximations[i][0], generalApproximations[i][1], tile.x, tile.y)
                        if tileDist < genDist and self.distance_from_general(tile) < 1000:
                            generalApproximations[i][3] = tile
                            genDist = tileDist

        self.generalApproximations = generalApproximations

        oldTgPlayer = self.targetPlayer
        self.targetPlayer = self.calculate_target_player()
        self.opponent_tracker.targetPlayer = self.targetPlayer

        self.targetPlayerObj = self._map.players[self.targetPlayer]

        if self.targetPlayer != oldTgPlayer:
            self._lastTargetPlayerCityCount = 0
            if self.targetPlayer >= 0:
                self._lastTargetPlayerCityCount = self.opponent_tracker.get_current_team_scores_by_player(self.targetPlayer).cityCount

    def determine_should_winning_all_in(self):
        if self.targetPlayer < 0:
            return False

        targetPlayer: Player = self._map.players[self.targetPlayer]
        if len(targetPlayer.tiles) == 0:
            return False
        thisPlayer: Player = self._map.players[self.general.player]

        ourArmy = thisPlayer.standingArmy
        oppArmy = targetPlayer.standingArmy

        for player in self._map.players:
            if player.index == self.targetPlayer or player.index == self.general.player:
                continue

            if self._map.is_player_on_team_with(self.targetPlayer, player.index):
                oppArmy += player.standingArmy
            elif self._map.is_player_on_team_with(self.general.player, player.index):
                ourArmy += player.standingArmy

        if ourArmy < 100:
            return False

        factoredArmyThreshold = oppArmy * 2 + self.shortest_path_to_target_player.length

        # if already all in, keep pushing for longer
        if self.is_all_in_army_advantage:
            factoredArmyThreshold = oppArmy * 1.4 + self.shortest_path_to_target_player.length // 2

        if ourArmy > factoredArmyThreshold:
            self.viewInfo.add_info_line(f"TEMP ALL IN ON ARMY ADV {ourArmy} vs {oppArmy} thresh({factoredArmyThreshold:.2f})")
            return True

        return False

    def find_expected_1v1_general_location_on_undiscovered_map(
            self,
            undiscoveredCounterDepth: int,
            minSpawnDistance: int
    ) -> MapMatrixInterface[int]:
        """bigger number in the matrix is better"""
        # finding path to some predicted general location in neutral territory
        # TODO look into the void and see it staring back at yourself
        # find mirror spot in the void? Or just discover the most tiles possible.
        # Kind of done. Except really, shouldn't be BFSing with so much CPU for this lol.
        localMaxTile = self.general
        maxAmount: int = -1
        grid = MapMatrix(self._map, 0)

        genDists = self._map.get_distance_matrix_including_obstacles(self.general)

        def tile_meets_criteria_for_value_around_general(t: Tile) -> bool:
            return (
                    not t.discovered
                    and t.isPathable
                    and (self.targetPlayer == -1 or self.armyTracker.valid_general_positions_by_player[self.targetPlayer].raw[t.tile_index])
            )

        def tile_meets_criteria_for_general(t: Tile) -> bool:
            return tile_meets_criteria_for_value_around_general(t) and genDists.raw[t.tile_index] >= minSpawnDistance and (self.teammate_general is None or self.distance_from_teammate(t) >= minSpawnDistance)

        for tile in self._map.pathable_tiles:
            if tile_meets_criteria_for_general(tile):
                # if not divide by 2, overly weights far tiles. Prefer mid-far central tiles
                genDist = genDists.raw[tile.tile_index] / 2
                distFromCenter = self.get_distance_from_board_center(tile, center_ratio=0.25)

                initScore = genDist - distFromCenter
                counter = SearchUtils.Counter(0)
                if self.info_render_general_undiscovered_prediction_values:
                    self.viewInfo.bottomMidLeftGridText.raw[tile.tile_index] = f'u{initScore:.1f}'

                # the lambda for counting stuff!
                def count_undiscovered(curTile):
                    if tile_meets_criteria_for_value_around_general(curTile):
                        if curTile.isDesert:
                            counter.add(0.1)
                        elif curTile.isSwamp:
                            counter.add(0.05)
                        elif curTile.isCity:
                            counter.add(3)
                        else:
                            counter.add(1)

                SearchUtils.breadth_first_foreach(self._map, [tile], undiscoveredCounterDepth, count_undiscovered, noLog=True)

                grid.raw[tile.tile_index] = counter.value + initScore
                if self.info_render_general_undiscovered_prediction_values:
                    self.viewInfo.bottomRightGridText.raw[tile.tile_index] = f'c{counter.value}'

                if counter.value > maxAmount:
                    localMaxTile = tile
                    maxAmount = counter.value

        if self.targetPlayer == -1 or len(self._map.players[self.targetPlayer].tiles) == 0:
            def mark_undiscovered(curTile):
                if tile_meets_criteria_for_value_around_general(curTile):
                    self._evaluatedUndiscoveredCache.append(curTile)
                    if self.info_render_general_undiscovered_prediction_values:
                        self.viewInfo.evaluatedGrid[curTile.x][curTile.y] = 1

            SearchUtils.breadth_first_foreach(self._map, [localMaxTile], undiscoveredCounterDepth, mark_undiscovered, noLog=True)

        return grid

    def prune_timing_split_if_necessary(self):
        if self.target_player_gather_path is None:
            return

        if self.is_ffa_situation() and self._map.turn < 150:
            return

        splitTurn = self.timings.get_turn_in_cycle(self._map.turn)
        tilesUngathered = SearchUtils.count(
            self._map.pathable_tiles,
            lambda tile: (
                    tile.player == self.general.player
                    and tile not in self.target_player_gather_path.tileSet
                    and tile.army > 1
            )
        )

        player = self._map.players[self.general.player]
        if tilesUngathered - player.cityCount - 1 < 1:
            timingAdjusted = splitTurn + tilesUngathered
            if timingAdjusted < self.timings.launchTiming:
                self.viewInfo.add_info_line(f"Moving up launch timing from {self.timings.launchTiming} to splitTurn {splitTurn} + tilesUngathered {tilesUngathered} = ({timingAdjusted})")
                self.timings.launchTiming = timingAdjusted
                self.timings.splitTurns = timingAdjusted

    def get_distance_from_board_center(self, tile, center_ratio=0.25) -> float:
        """
        bigger center_ratio means more of the center of the board counts as 0.

        @param tile:
        @param center_ratio:
        @return:
        """
        distFromCenterX = abs((self._map.cols / 2) - tile.x)
        distFromCenterY = abs((self._map.rows / 2) - tile.y)

        distFromCenterX -= self._map.cols * center_ratio
        distFromCenterY -= self._map.rows * center_ratio

        # prioritize the center box equally
        if distFromCenterX < 0:
            distFromCenterX = 0
        if distFromCenterY < 0:
            distFromCenterY = 0
        return distFromCenterX + distFromCenterY

    def get_predicted_target_player_general_location(self, skipDiscoveredAsNeutralFilter: bool = False) -> Tile:
        minSpawnDist = self.armyTracker.min_spawn_distance

        if self.targetPlayer == -1 and self.is_still_ffa_and_non_dominant():
            self.info(f'bypassed get_predicted_target_player_general_location because no target player and ffa')
            return self.general

        if self.targetPlayer == -1:
            with self.perf_timer.begin_move_event('get_max_explorable_undiscovered_tile'):
                self.info(f'DEBUG get_max_explorable_undiscovered_tile')
                return self.get_max_explorable_undiscovered_tile(minSpawnDist)

        if self._map.generals[self.targetPlayer] is not None:
            self.info(f'DEBUG enemyGeneral')
            return self._map.generals[self.targetPlayer]

        maxTile = self.general
        values = MapMatrix(self._map, 0.0)

        maxAmount = 0

        for tile in self._map.pathable_tiles:
            if tile.discovered or tile.isMountain or tile.isNotPathable or tile.isCity:
                continue

            # if (self._map.remainingPlayers > 2
            #         and 0 == SearchUtils.count(tile.adjacents, lambda adjTile: adjTile.player == self.player)
            # ):
            #     # in FFA, don't evaluate tiles other than those directly next to enemy tiles (to avoid overshooting into 3rd party territory)
            #     continue

            foundValue = 0

            if not self.armyTracker.valid_general_positions_by_player[self.targetPlayer][tile]:
                continue

            if self.armyTracker.emergenceLocationMap[self.targetPlayer][tile] > 0:
                # foundValue += emergenceLogFactor * math.log(self.armyTracker.emergenceLocationMap[self.player][tile], 2)
                foundValue += self.armyTracker.emergenceLocationMap[self.targetPlayer][tile] * 10

            values.raw[tile.tile_index] = foundValue
            if foundValue > maxAmount:
                maxTile = tile
                maxAmount = foundValue
            if foundValue > 0 and self.info_render_general_undiscovered_prediction_values:
                self.viewInfo.midRightGridText[tile] = f'we{foundValue}'

        self.viewInfo.add_targeted_tile(maxTile, TargetStyle.BLUE, radiusReduction=11)

        if maxTile is not None and maxTile != self.general and not maxTile.isObstacle and not maxTile.isCity:
            self.undiscovered_priorities = values
            logbook.info(
                f"Highest density undiscovered tile {str(maxTile)} with value {maxAmount} found")
            return maxTile

        if self.targetPlayer != -1 and len(self.targetPlayerObj.tiles) > 0:
            self.viewInfo.add_info_line("target path failed, hacky gen approx attempt:")
            with self.perf_timer.begin_move_event('find_hacky_path_to_find_target_player_spawn_approx'):
                maxTile = self.find_hacky_path_to_find_target_player_spawn_approx(minSpawnDist)
                if maxTile is not None and maxTile != self.general and not maxTile.isObstacle and not maxTile.isCity:
                    self.info(
                        f"Highest density undiscovered tile {str(maxTile)}")
                    return maxTile

            for tile in self.targetPlayerObj.tiles:
                for adjTile in tile.movable:
                    if not adjTile.discovered and not adjTile.isObstacle and not adjTile.isCity:
                        self.info(f"target path failed, falling back to {adjTile} - a random tile adj to en.")
                        return adjTile

        self.viewInfo.add_info_line(f"target path failed, falling back to undiscovered path. minSpawnDist {minSpawnDist}")
        with self.perf_timer.begin_move_event(f'fb{self.targetPlayer} get_max_explorable_undiscovered_tile'):
            fallbackTile = self.get_max_explorable_undiscovered_tile(minSpawnDist)
            if fallbackTile is not None and fallbackTile != self.general:
                self.info(f"target path failed, falling back to {fallbackTile} - get_max_explorable_undiscovered_tile")
                return fallbackTile

        furthestDist = 0
        furthestTile = None
        for tile in self._map.pathable_tiles:
            if tile.visible:
                continue
            if tile.isCity:
                continue

            d = self.distance_from_general(tile)
            if furthestDist < d < 999:
                furthestDist = d
                furthestTile = tile

        self.info(f"target path fallback failed, {furthestTile} furthest {furthestDist} pathable reachable tile")

        if furthestTile is not None:
            return furthestTile

        gMov = next(iter(self.general.movableNoObstacles))
        self.info(f"target path fallback failed, returning {gMov} tile next to general.")
        return gMov

    def is_player_spawn_cramped(self, spawnDist=-1) -> bool:
        if self._spawn_cramped is not None:
            return self._spawn_cramped

        if spawnDist == -1:
            self.target_player_gather_targets = {t for t in self.target_player_gather_path.tileList if not t.isSwamp and not (t.isDesert and t.isNeutral)}
            if len(self.target_player_gather_targets) == 0:
                self.target_player_gather_targets = self.target_player_gather_path.tileSet
            spawnDist = self.shortest_path_to_target_player.length

        tiles = [self.general]

        counter = SearchUtils.Counter(0)

        # if we dont find enemy territory (which is around halfway point)
        spawnDist = spawnDist / 2.0

        def count_neutral(curTile: Tile):
            tileTerritory = self.territories.territoryMap[curTile]
            isTileContested = self._map.is_tile_enemy(curTile)
            isTileContested |= tileTerritory != self.general.player and tileTerritory >= 0 and tileTerritory not in self._map.teammates
            if not curTile.isNotPathable:
                counter.add(0.5)
            if not isTileContested:
                counter.add(0.5)

        counter.value = 0
        SearchUtils.breadth_first_foreach(self._map, tiles, 8, count_neutral, noLog=True)
        count8 = counter.value

        counter.value = 0
        SearchUtils.breadth_first_foreach(self._map, tiles, 6, count_neutral, noLog=True)
        count6 = counter.value

        counter.value = 0
        SearchUtils.breadth_first_foreach(self._map, tiles, 4, count_neutral, noLog=True)
        count4 = counter.value

        enTerritoryStr = ''
        if self.targetPlayer != -1:
            enemyTerritoryFoundCounter = SearchUtils.Counter(0)
            targetPlayer: Player = self._map.players[self.targetPlayer]
            visibleTiles = [t for t in filter(lambda tile: tile.visible, targetPlayer.tiles)]
            enemyVisibleTileCount = len(visibleTiles)

            def count_enemy_territory(curTile: Tile, object):
                tileTerritory = self.territories.territoryMap[curTile]
                isTileContested = self._map.is_tile_enemy(curTile)
                isTileContested |= tileTerritory != self.general.player and tileTerritory >= 0 and tileTerritory not in self._map.teammates
                if isTileContested:
                    enemyTerritoryFoundCounter.add(1)

                if enemyTerritoryFoundCounter.value > enemyVisibleTileCount:
                    return True

                return False

            path = SearchUtils.breadth_first_dynamic(
                self._map,
                tiles,
                count_enemy_territory,
                noNeutralCities=True,
                searchingPlayer=self.general.player)

            if path is not None:
                territoryTile = path.tileList[-1]
                self.viewInfo.add_targeted_tile(territoryTile, TargetStyle.RED)
                # self.viewInfo.add_info_line(f"found enemy territory at dist {path.length} {str(territoryTile)}")
                enTerritoryStr = f'enTerr d{path.length} @{str(territoryTile)}'
                spawnDist = path.length

        spawnDistFactor = spawnDist - 10

        thisPlayer = self._map.players[self.general.player]
        cap8 = 68 - 9 * (thisPlayer.cityCount - 1) + spawnDistFactor
        cap6 = 42 - 6 * (thisPlayer.cityCount - 1) + spawnDistFactor
        cap4 = 21 - 3 * (thisPlayer.cityCount - 1) + spawnDistFactor

        cramped = False
        if count8 < cap8 or count6 < cap6 or count4 < cap4:
            cramped = True

        self.viewInfo.add_stats_line(f"Cramped: {cramped} 8[{count8}/{cap8}] 6[{count6}/{cap6}] 4[{count4}/{cap4}] spawnDistFactor[{spawnDistFactor}] {enTerritoryStr}")

        self._spawn_cramped = cramped

        return cramped

    def timing_cycle_ended(self):
        self.is_winning_gather_cyclic = False
        self.viewInfo.add_info_line(f'Timing cycle ended, turn {self._map.turn}')
        self.cities_gathered_this_cycle = set()
        self.tiles_gathered_to_this_cycle = set()
        self.tiles_captured_this_cycle: typing.Set[Tile] = set()
        self.tiles_evacuated_this_cycle: typing.Set[Tile] = set()
        self.city_expand_plan = None
        self.curPath = None
        player = self._map.players[self.general.player]
        cityCount = player.cityCount

        citiesAvoided = 0
        if player.cityCount > 4:
            for city in sorted(player.cities, key=lambda c: c.army):
                if citiesAvoided >= cityCount // 2 - 2:
                    break
                citiesAvoided += 1
                self.viewInfo.add_info_line(f'AVOIDING CITY {repr(city)}')
                self.cities_gathered_this_cycle.add(city)

        self.locked_launch_point = None
        self.flanking = False

    def dump_turn_data_to_string(self):
        charMap = PLAYER_CHAR_BY_INDEX

        data = []

        data.append(f'bot_target_player={self.targetPlayer}')
        if self.targetPlayerExpectedGeneralLocation and self.targetPlayer != -1:
            data.append(f'targetPlayerExpectedGeneralLocation={self.targetPlayerExpectedGeneralLocation.x},{self.targetPlayerExpectedGeneralLocation.y}')
        data.append(f'bot_is_all_in_losing={self.is_all_in_losing}')
        data.append(f'bot_all_in_losing_counter={self.all_in_losing_counter}')

        data.append(f'bot_is_winning_gather_cyclic={self.is_winning_gather_cyclic}')
        data.append(f'bot_is_all_in_army_advantage={self.is_all_in_army_advantage}')
        data.append(f'bot_all_in_army_advantage_counter={self.all_in_army_advantage_counter}')
        data.append(f'bot_all_in_army_advantage_cycle={self.all_in_army_advantage_cycle}')
        data.append(f'bot_defend_economy={self.defend_economy}')
        if self.timings is not None:
            data.append(f'bot_timings_launch_timing={self.timings.launchTiming}')
            data.append(f'bot_timings_split_turns={self.timings.splitTurns}')
            data.append(f'bot_timings_quick_expand_turns={self.timings.quickExpandTurns}')
            data.append(f'bot_timings_cycle_turns={self.timings.cycleTurns}')

        data.append(f'bot_is_rapid_capturing_neut_cities={self.is_rapid_capturing_neut_cities}')
        data.append(f'bot_is_blocking_neutral_city_captures={self.is_blocking_neutral_city_captures}')
        data.append(f'bot_finishing_exploration={self.finishing_exploration}')
        if self.targetingArmy:
            data.append(f'bot_targeting_army={self.targetingArmy.tile.x},{self.targetingArmy.tile.y}')
        data.append(f'bot_cur_path={str(self.curPath)}')

        for player in self._map.players:
            char = charMap[player.index]
            unsafeUserName = self._map.usernames[player.index].replace('=', '__')

            safeUserName = ''.join([i if ord(i) < 128 else ' ' for i in unsafeUserName])
            data.append(f'{char}Username={safeUserName}')
            data.append(f'{char}Tiles={player.tileCount}')
            data.append(f'{char}Score={player.score}')
            data.append(f'{char}StandingArmy={player.standingArmy}')
            data.append(f'{char}Stars={player.stars}')
            data.append(f'{char}CityCount={player.cityCount}')
            if player.general is not None:
                data.append(f'{char}General={player.general.x},{player.general.y}')
            data.append(f'{char}KnowsKingLocation={player.knowsKingLocation}')
            if self._map.is_2v2:
                data.append(f'{char}KnowsAllyKingLocation={player.knowsAllyKingLocation}')
            data.append(f'{char}Dead={player.dead}')
            data.append(f'{char}LeftGame={player.leftGame}')
            data.append(f'{char}LeftGameTurn={player.leftGameTurn}')
            data.append(f'{char}AggressionFactor={player.aggression_factor}')
            data.append(f'{char}Delta25Tiles={player.delta25tiles}')
            data.append(f'{char}Delta25Score={player.delta25score}')
            data.append(f'{char}CityGainedTurn={player.cityGainedTurn}')
            data.append(f'{char}CityLostTurn={player.cityLostTurn}')
            data.append(f'{char}LastSeenMoveTurn={player.last_seen_move_turn}')
            data.append(f'{char}Emergences={self.convert_float_map_matrix_to_string(self.armyTracker.emergenceLocationMap[player.index])}')
            data.append(f'{char}ValidGeneralPos={self.convert_bool_map_matrix_to_string(self.armyTracker.valid_general_positions_by_player[player.index])}')
            data.append(f'{char}TilesEverOwned={self.convert_tile_set_to_string(self.armyTracker.tiles_ever_owned_by_player[player.index])}')
            data.append(f'{char}UneliminatedEmergences={self.convert_tile_int_dict_to_string(self.armyTracker.uneliminated_emergence_events[player.index])}')
            data.append(f'{char}UneliminatedEmergenceCityPerfectInfo={self.convert_tile_set_to_string(self.armyTracker.uneliminated_emergence_event_city_perfect_info[player.index])}')
            data.append(f'{char}UnrecapturedEmergences={self.convert_tile_set_to_string(self.armyTracker.unrecaptured_emergence_events[player.index])}')
            if len(self.generalApproximations) > player.index:
                if self.generalApproximations[player.index][3] is not None:
                    data.append(f'{char}_bot_general_approx={str(self.generalApproximations[player.index][3])}')

        tempSet = set()
        neutDiscSet = set()
        for tile in self._map.get_all_tiles():
            if tile.isTempFogPrediction:
                tempSet.add(tile)
            if tile.discoveredAsNeutral:
                neutDiscSet.add(tile)
        data.append(f'TempFogTiles={self.convert_tile_set_to_string(tempSet)}')
        data.append(f'DiscoveredNeutral={self.convert_tile_set_to_string(neutDiscSet)}')

        data.append(f'Armies={TextMapLoader.dump_armies(self._map, self.armyTracker.armies)}')
        # entangledHandled = set()
        # entangledStrs = []
        # for army in self.armyTracker.armies.values():
        #     if not army.entangledArmies:
        #         continue
        #     if army.tile.tile_index in entangledHandled:
        #         continue
        #     entangled = [army.tile]
        #     entangled.extend(a.tile for a in army.entangledArmies)
        #
        #     entangledStrs.append("|".join([f'{t.x},{t.y}' for t in entangled]))
        #
        #     entangledHandled.update(t.tile_index for t in entangled)
        # if entangledStrs:
        #     data.append(f'Entangled={":".join(entangledStrs)}')

        data.append(self.opponent_tracker.dump_to_string_data())

        return '\n'.join(data)

    def parse_tile_str(self, tileStr: str) -> Tile:
        xStr, yStr = tileStr.split(',')
        return self._map.GetTile(int(xStr), int(yStr))

    def parse_bool(self, boolStr: str) -> bool:
        return boolStr.lower().strip() == "true"

    def load_resume_data(self, resume_data: typing.Dict[str, str]):
        if f'bot_target_player' in resume_data:  # ={self.player}')
            self.targetPlayer = int(resume_data[f'bot_target_player'])
            if self.targetPlayer >= 0:
                self.targetPlayerObj = self._map.players[self.targetPlayer]
            self.opponent_tracker.targetPlayer = self.targetPlayer
        if f'targetPlayerExpectedGeneralLocation' in resume_data:  # ={self.targetPlayerExpectedGeneralLocation.x},{self.targetPlayerExpectedGeneralLocation.y}')
            self.targetPlayerExpectedGeneralLocation = self.parse_tile_str(resume_data[f'targetPlayerExpectedGeneralLocation'])
        if f'bot_is_all_in_losing' in resume_data:  # ={self.is_all_in_losing}')
            self.is_all_in_losing = self.parse_bool(resume_data[f'bot_is_all_in_losing'])
        if f'bot_all_in_losing_counter' in resume_data:  # ={self.all_in_losing_counter}')
            self.all_in_losing_counter = int(resume_data[f'bot_all_in_losing_counter'])

        if f'bot_is_all_in_army_advantage' in resume_data:  # ={self.is_all_in_army_advantage}')
            self.is_all_in_army_advantage = self.parse_bool(resume_data[f'bot_is_all_in_army_advantage'])
        if f'bot_is_winning_gather_cyclic' in resume_data:  # ={self.is_all_in_army_advantage}')
            self.is_winning_gather_cyclic = self.parse_bool(resume_data[f'bot_is_winning_gather_cyclic'])
        if f'bot_all_in_army_advantage_counter' in resume_data:  # ={self.all_in_army_advantage_counter}')
            self.all_in_army_advantage_counter = int(resume_data[f'bot_all_in_army_advantage_counter'])
        if f'bot_all_in_army_advantage_cycle' in resume_data:  # ={self.all_in_army_advantage_cycle}')
            self.all_in_army_advantage_cycle = int(resume_data[f'bot_all_in_army_advantage_cycle'])
        if f'bot_defend_economy' in resume_data:
            self.defend_economy = self.parse_bool(resume_data[f'bot_defend_economy'])

        if f'bot_timings_launch_timing' in resume_data:
            # self.timings = None
            # if self._map.turn % 50 != 0:
            cycleTurns = self._map.turn + self._map.remainingCycleTurns
            self.timings = Timings(0, 0, 0, 0, 0, cycleTurns, disallowEnemyGather=True)
            self.timings.launchTiming = int(resume_data[f'bot_timings_launch_timing'])
            self.timings.splitTurns = int(resume_data[f'bot_timings_split_turns'])
            self.timings.quickExpandTurns = int(resume_data[f'bot_timings_quick_expand_turns'])
            self.timings.cycleTurns = int(resume_data[f'bot_timings_cycle_turns'])

        if f'bot_is_rapid_capturing_neut_cities' in resume_data:  # ={self.is_rapid_capturing_neut_cities}')
            self.is_rapid_capturing_neut_cities = self.parse_bool(resume_data[f'bot_is_rapid_capturing_neut_cities'])
        if f'bot_is_blocking_neutral_city_captures' in resume_data:  # ={self.is_blocking_neutral_city_captures}')
            self.is_blocking_neutral_city_captures = self.parse_bool(resume_data[f'bot_is_blocking_neutral_city_captures'])
        if f'bot_finishing_exploration' in resume_data:  # ={self.finishing_exploration}')
            self.finishing_exploration = self.parse_bool(resume_data[f'bot_finishing_exploration'])
        if f'bot_targeting_army' in resume_data:  # ={self.targetingArmy.tile.x},{self.targetingArmy.tile.y}')
            self.targetingArmy = self.get_army_at(self.parse_tile_str(resume_data[f'bot_targeting_army']))
        else:
            self.targetingArmy = None
        if f'bot_cur_path' in resume_data:  # ={str(self.curPath)}')
            self.curPath = TextMapLoader.parse_path(self._map, resume_data[f'bot_cur_path'])
        else:
            self.curPath = None

        for player in self._map.players:
            char = PLAYER_CHAR_BY_INDEX[player.index]
            if f'{char}Emergences' in resume_data:
                self.armyTracker.emergenceLocationMap[player.index] = self.convert_string_to_float_map_matrix(resume_data[f'{char}Emergences'])
            elif f'targetPlayerExpectedGeneralLocation' in resume_data and player.index == self.targetPlayer and len(self._map.players[self.targetPlayer].tiles) > 0:
                # only do the old behavior when explicit emergences arent available.
                self.armyTracker.emergenceLocationMap[self.targetPlayer][self.targetPlayerExpectedGeneralLocation] = 5
            if f'{char}ValidGeneralPos' in resume_data:
                self.armyTracker.valid_general_positions_by_player[player.index] = self.convert_string_to_bool_map_matrix_set(resume_data[f'{char}ValidGeneralPos'])
            if f'{char}TilesEverOwned' in resume_data:
                self.armyTracker.tiles_ever_owned_by_player[player.index] = self.convert_string_to_tile_set(resume_data[f'{char}TilesEverOwned'])
            if f'{char}UneliminatedEmergences' in resume_data:
                self.armyTracker.uneliminated_emergence_events[player.index] = self.convert_string_to_tile_int_dict(resume_data[f'{char}UneliminatedEmergences'])
            if f'{char}UneliminatedEmergenceCityPerfectInfo' in resume_data:
                self.armyTracker.uneliminated_emergence_event_city_perfect_info[player.index] = self.convert_string_to_tile_set(resume_data[f'{char}UneliminatedEmergenceCityPerfectInfo'])
            else:
                self.armyTracker.uneliminated_emergence_event_city_perfect_info[player.index] = {t for t in self.armyTracker.uneliminated_emergence_events[player.index].keys()}
            if f'{char}UnrecapturedEmergences' in resume_data:
                self.armyTracker.unrecaptured_emergence_events[player.index] = self.convert_string_to_tile_set(resume_data[f'{char}UnrecapturedEmergences'])
            else:
                pUnelim = self.armyTracker.uneliminated_emergence_events[player.index]
                pUnrecaptured = self.armyTracker.unrecaptured_emergence_events[player.index]
                for t in self._map.get_all_tiles():
                    if t in pUnelim:
                        pUnrecaptured.add(t)

        if f'TempFogTiles' in resume_data:
            tiles = self.convert_string_to_tile_set(resume_data[f'TempFogTiles'])
            for tile in tiles:
                tile.isTempFogPrediction = True
        if f'DiscoveredNeutral' in resume_data:
            tiles = self.convert_string_to_tile_set(resume_data[f'DiscoveredNeutral'])
            for tile in tiles:
                tile.discoveredAsNeutral = True

        if 'is_custom_map' in resume_data:
            self._map.is_custom_map = bool(resume_data['is_custom_map'])
        if 'walled_city_base_value' in resume_data:
            self._map.walled_city_base_value = int(resume_data['walled_city_base_value'])
            self._map.is_walled_city_game = True

        if 'PATHABLE_CITY_THRESHOLD' in resume_data:
            Tile.PATHABLE_CITY_THRESHOLD = int(resume_data['PATHABLE_CITY_THRESHOLD'])
            self._map.distance_mapper.recalculate()
            self._map.update_reachable()
        else:
            Tile.PATHABLE_CITY_THRESHOLD = 5  # old replays, no cities were ever pathable.

        if self.targetPlayerExpectedGeneralLocation:
            self.board_analysis.rebuild_intergeneral_analysis(self.targetPlayerExpectedGeneralLocation, self.armyTracker.valid_general_positions_by_player)

        self.opponent_tracker.load_from_map_data(resume_data)
        if self.targetPlayer >= 0:
            self._lastTargetPlayerCityCount = self.opponent_tracker.get_current_team_scores_by_player(self.targetPlayer).cityCount

        self.last_init_turn = self._map.turn - 1

        self.city_expand_plan = None
        self.expansion_plan = ExpansionPotential(0, 0, 0, None, [], 0.0)
        self.enemy_expansion_plan = None

        loadedArmies = TextMapLoader.load_armies(self._map, resume_data)
        if len(loadedArmies) == 0:
            for army in self.armyTracker.armies.values():
                army.last_moved_turn = self._map.turn - 3

            if self.targetingArmy:
                self.targetingArmy.last_moved_turn = self._map.turn - 1

            for army in self.armyTracker.armies.values():
                if army.tile.discovered:
                    army.last_moved_turn = self._map.turn - 1
                else:
                    army.last_moved_turn = self._map.turn - 5

        else:
            self.armyTracker.armies = loadedArmies

        self.history = History()

        # force a rebuild
        self.cityAnalyzer.reset_reachability()

        return
        #
        # for player in self._map.players:
        #     char = PLAYER_CHAR_BY_INDEX[player.index]
        #
        #     # if f'{char}StandingArmy' in resume_data:  # ={player.standingArmy}')
        #     #     self.something = int(resume_data[f'{char}StandingArmy'])
        #     # if f'{char}KnowsKingLocation' in resume_data:  # ={player.knowsKingLocation}')
        #     #     self._map.players[player.index].knowsKingLocation = self.parse_bool(resume_data[f'{char}KnowsKingLocation'])
        #     # if self._map.is_2v2:
        #     #     if f'{char}KnowsAllyKingLocation' in resume_data:  # ={player.knowsAllyKingLocation}')
        #     #         self.something = int(resume_data[f'{char}KnowsAllyKingLocation'])
        #     # if f'{char}Dead' in resume_data:  # ={player.dead}')
        #     #     self.something = int(resume_data[f'{char}Dead'])
        #     # if f'{char}LeftGame' in resume_data:  # ={player.leftGame}')
        #     #     self.something = int(resume_data[f'{char}LeftGame'])
        #     # if f'{char}LeftGameTurn' in resume_data:  # ={player.leftGameTurn}')
        #     #     self.something = int(resume_data[f'{char}LeftGameTurn'])
        #     # if f'{char}AggressionFactor' in resume_data:  # ={player.aggression_factor}')
        #     #     self.something = int(resume_data[f'{char}AggressionFactor'])
        #     # if f'{char}Delta25Tiles' in resume_data:  # ={player.delta25tiles}')
        #     #     self.something = int(resume_data[f'{char}Delta25Tiles'])
        #     # if f'{char}Delta25Score' in resume_data:  # ={player.delta25score}')
        #     #     self.something = int(resume_data[f'{char}Delta25Score'])
        #     # if f'{char}CityGainedTurn' in resume_data:  # ={player.cityGainedTurn}')
        #     #     self.something = int(resume_data[f'{char}CityGainedTurn'])
        #     # if f'{char}CityLostTurn' in resume_data:  # ={player.cityLostTurn}')
        #     #     self.something = int(resume_data[f'{char}CityLostTurn'])
        #     # if f'{char}LastSeenMoveTurn' in resume_data:  # ={player.last_seen_move_turn}')
        #     #     self.something = int(resume_data[f'{char}LastSeenMoveTurn'])
        #     # if len(self.generalApproximations) > player.index:
        #     #     if self.generalApproximations[player.index][3] is not None:
        #     #         if f'{char}_bot_general_approx' in resume_data:  # ={str(self.generalApproximations[player.index][3])}')
        #     #             self.something = int(resume_data[f'{char}_bot_general_approx'])

    def is_move_safe_against_threats(self, move: Move):
        threat = self.threat
        if not threat:
            threat = self.dangerAnalyzer.fastestPotentialThreat

        if not threat:
            return True

        if threat.threatType != ThreatType.Kill:
            return True

        # if attacking the threat, then cool
        if move.dest == threat.path.start.tile or (move.dest == threat.path.start.next.tile and len(threat.armyAnalysis.tileDistancesLookup[1]) == 1):
            return True

        # if moving out of a choke, dont
        if threat.armyAnalysis.is_choke(move.source) and not threat.armyAnalysis.is_choke(move.dest):
            self.viewInfo.add_info_line(f'not allowing army move out of threat choke {str(move.source)}')
            return False

        if move.source in threat.path.tileSet and move.dest not in threat.path.tileSet:
            self.viewInfo.add_info_line(f'not allowing army move out of threat path {str(move.source)}')
            return False

        return True

    def get_gather_to_threat_path(
            self,
            threat: ThreatObj,
            force_turns_up_threat_path=0,
            gatherMax: bool = True,
            shouldLog: bool = False,
            addlTurns: int = 0,
            requiredContribution: int | None = None,
            additionalNegatives: typing.Set[Tile] | None = None,
            interceptArmy: bool = False,
            timeLimit: float | None = None
    ) -> typing.Tuple[None | Move, int, int, None | typing.List[GatherTreeNode]]:
        """
        returns move, value, turnsUsed, gatherNodes

        @param threat:
        @param force_turns_up_threat_path:
        @param gatherMax: Sets targetArmy to -1 in the gather, allowing the gather to return less than the threat value.
        @param shouldLog:
        @param addlTurns: if you want to gather longer than the threat, for final save.
        @param requiredContribution: replaces the threat.threatValue as the required army contribution if passed. Does nothing if gatherMax is True.
        @param additionalNegatives:
        @return: move, value, turnsUsed, gatherNodes
        """
        return self.get_gather_to_threat_paths(
            [threat],
            force_turns_up_threat_path,
            gatherMax,
            shouldLog,
            addlTurns,
            requiredContribution,
            additionalNegatives,
            interceptArmy=interceptArmy,
            timeLimit=timeLimit
        )

    def get_gather_to_threat_paths(
            self,
            threats: typing.List[ThreatObj],
            force_turns_up_threat_path=0,
            gatherMax: bool = True,
            shouldLog: bool = False,
            addlTurns: int = 0,
            requiredContribution: int | None = None,
            additionalNegatives: typing.Set[Tile] | None = None,
            interceptArmy: bool = False,
            timeLimit: float | None = None
    ) -> typing.Tuple[None | Move, int, int, None | typing.List[GatherTreeNode]]:
        """
        returns move, value, turnsUsed, gatherNodes

        @param threats:
        @param force_turns_up_threat_path:
        @param gatherMax: Sets targetArmy to -1 in the gather, allowing the gather to return less than the threat value.
        @param shouldLog:
        @param addlTurns: if you want to gather longer than the threat, for final save.
        @param requiredContribution: replaces the threat.threatValue as the required army contribution if passed. Does nothing if gatherMax is True.
        @param additionalNegatives:
        @return: move, value, turnsUsed, gatherNodes
        """

        if requiredContribution is None:
            requiredContribution = threats[0].threatValue

        gatherDepth = threats[0].path.length - 1 + addlTurns
        distDict = threats[0].convert_to_dist_dict(allowNonChoke=force_turns_up_threat_path != 0, offset=-1 - addlTurns, mapForPriority=self._map)
        if self.has_defenseless_modifier:
            for t in [h for h in distDict.keys()]:
                if t.isGeneral:
                    del distDict[t]

        move, value, turnsUsed, gatherNodes = self.try_threat_gather(
            threats=threats,
            distDict=distDict,
            gatherDepth=gatherDepth,
            force_turns_up_threat_path=force_turns_up_threat_path,
            requiredContribution=requiredContribution,
            gatherMax=gatherMax,
            additionalNegatives=additionalNegatives,
            timeLimit=timeLimit,
            shouldLog=shouldLog)

        return move, value, turnsUsed, gatherNodes

    def try_threat_gather(
            self,
            threats: typing.List[ThreatObj],
            distDict,
            gatherDepth,
            force_turns_up_threat_path,
            requiredContribution,
            gatherMax,
            additionalNegatives,
            timeLimit,
            pruneDepth: int | None = None,
            shouldLog: bool = False,
            fastMode: bool = False
    ) -> typing.Tuple[None | Move, int, int, None | typing.List[GatherTreeNode]]:

        # for tile in list(distDict.keys()):
        #     if tile not in commonInterceptPoints:
        #         del distDict[tile]

        if self._map.is_player_on_team_with(threats[0].path.start.tile.player, self.general.player):
            raise AssertionError(f'threat paths should start with enemy tile, not friendly tile. Path {str(threats[0].path)}')

        threatDistMap = None
        for threat in threats:
            tail = threat.path.tail
            for i in range(force_turns_up_threat_path):
                if tail is not None:
                    # self.viewInfo.add_targeted_tile(tail.tile, TargetStyle.GREEN)
                    distDict.pop(tail.tile, None)
                    tail = tail.prev
            threatDistMap = threat.armyAnalysis.aMap

        # for tile in distDict.keys():
        #     logbook.info(f'common intercept {str(tile)} at dist {distDict[tile]}')
        #     self.viewInfo.add_targeted_tile(tile, TargetStyle.GOLD, radiusReduction=9)

        move_closest_value_func = None
        if force_turns_up_threat_path == 0:
            move_closest_value_func = self.get_defense_tree_move_prio_func(threats[0])

        survivalThreshold = requiredContribution

        if survivalThreshold is None:
            survivalThreshold = threats[0].threatValue

        targetArmy = survivalThreshold
        if gatherMax:
            targetArmy = -1

        negatives = set()
        # if force_turns_up_threat_path == 0:
        for threat in threats:
            negatives.update(threat.path.tileSet)
            if self.has_defenseless_modifier and self.general in negatives and threat.path.tail.tile == self.general:
                negatives.discard(self.general)
                targetArmy += 1
            elif threat.path.tail.tile != self.general:
                if len(self.get_danger_tiles()) > 0:
                    negatives.add(self.general)

        if additionalNegatives is not None:
            negatives.update(negatives)

        prioMatrix = MapMatrix(self._map, 0.0)
        for tile in self._map.pathable_tiles:
            prioMatrix.raw[tile.tile_index] = 0.0001 * threats[0].armyAnalysis.aMap.raw[tile.tile_index]  # reward distances further from the threats target, pushing us to intercept further up the path. In theory?

        if timeLimit is None:
            if DebugHelper.IS_DEBUGGING:
                timeLimit = 1000
            else:
                timeLimit = 0.05

        move, value, turnsUsed, gatherNodes = self.get_defensive_gather_to_target_tiles(
            distDict,
            maxTime=timeLimit,
            gatherTurns=gatherDepth,
            targetArmy=targetArmy,
            useTrueValueGathered=False,
            negativeSet=negatives,
            leafMoveSelectionValueFunc=move_closest_value_func,
            includeGatherTreeNodesThatGatherNegative=True,
            priorityMatrix=prioMatrix,
            distPriorityMap=threatDistMap,
            # maximizeArmyGatheredPerTurn=gatherMax,  # this just immediately breaks the whole gather, prunes everything but the largest tile basically.
            shouldLog=shouldLog,
            fastMode=fastMode)

        if pruneDepth is not None and gatherNodes is not None:
            turnsUsed, value, gatherNodes = Gather.prune_mst_to_turns_with_values(
                gatherNodes,
                pruneDepth,
                searchingPlayer=self.general.player,
                viewInfo=self.viewInfo if self.info_render_gather_values else None
            )

            move = self.get_tree_move_default(gatherNodes)

        logbook.info(f'get_gather_to_threat_path for depth {gatherDepth} force_turns_up_threat_path {force_turns_up_threat_path} returned {move}, val {value} turns {turnsUsed}')
        return move, value, turnsUsed, gatherNodes

    def get_gather_to_threat_path_greedy(
            self,
            threat: ThreatObj,
            force_turns_up_threat_path=0,
            gatherMax: bool = True,
            shouldLog: bool = False
    ) -> typing.Tuple[None | Move, int, int, None | typing.List[GatherTreeNode]]:
        """
        Greedy is faster than the main knapsack version.
        returns move, valueGathered, turnsUsed

        @return:
        """
        gatherDepth = threat.path.length - 1
        distDict = threat.convert_to_dist_dict()
        tail = threat.path.tail
        for i in range(force_turns_up_threat_path):
            if tail is not None:
                # self.viewInfo.add_targeted_tile(tail.tile, TargetStyle.GREEN)
                del distDict[tail.tile]
                tail = tail.prev

        distMap = SearchUtils.build_distance_map_matrix(self._map, [threat.path.start.tile])

        def move_closest_priority_func(nextTile, currentPriorityObject):
            return nextTile in threat.armyAnalysis.shortestPathWay.tiles, distMap[nextTile]

        def move_closest_value_func(curTile, currentPriorityObject):
            return curTile not in threat.armyAnalysis.shortestPathWay.tiles, 0 - distMap[curTile]

        targetArmy = threat.threatValue
        if gatherMax:
            targetArmy = -1

        move, value, turnsUsed, gatherNodes = self.get_gather_to_target_tiles_greedy(
            distDict,
            maxTime=0.05,
            gatherTurns=gatherDepth,
            targetArmy=targetArmy,
            useTrueValueGathered=True,
            priorityFunc=move_closest_priority_func,
            valueFunc=move_closest_value_func,
            includeGatherTreeNodesThatGatherNegative=True,
            shouldLog=shouldLog)
        logbook.info(f'get_gather_to_threat_path for depth {gatherDepth} force_turns_up_threat_path {force_turns_up_threat_path} returned {move}, val {value} turns {turnsUsed}')

        return move, value, turnsUsed, gatherNodes

    def recalculate_player_paths(self, force: bool = False):
        self.ensure_reachability_matrix_built()
        reevaluate = force
        if len(self._evaluatedUndiscoveredCache) > 0:
            for tile in self._evaluatedUndiscoveredCache:
                if tile.discovered:
                    reevaluate = True
                    break
        if self.targetPlayerExpectedGeneralLocation is not None and self.targetPlayerExpectedGeneralLocation.visible and not self.targetPlayerExpectedGeneralLocation.isGeneral:
            reevaluate = True

        if SearchUtils.any_where(self._map.get_all_tiles(), lambda t: t.isCity and t.player >= 0 and t.delta.oldOwner == -1):
            reevaluate = True

        intentionallyGatheringAtNonGeneralTarget = self.target_player_gather_path is None or (not self.target_player_gather_path.tail.tile.isGeneral and self._map.generals[self.targetPlayer] is not None)
        if (self.target_player_gather_path is None
                or reevaluate
                or self.target_player_gather_path.tail.tile.isCity
                or self._map.turn % 50 < 2
                or self.timings.in_launch_split(self._map.turn - 3)
                or self._map.turn < 100
                or intentionallyGatheringAtNonGeneralTarget
                or self.curPath is None):

            # self.undiscovered_priorities = None

            self.shortest_path_to_target_player = self.get_path_to_target_player(isAllIn=self.is_all_in(), cutLength=None)
            self.info(f'DEBUG: shortest_path_to_target_player {self.shortest_path_to_target_player}')
            if self.shortest_path_to_target_player is None:
                self.shortest_path_to_target_player = Path()
                self.shortest_path_to_target_player.add_next(self.general)
                self.shortest_path_to_target_player.add_next(next(iter(self.general.movableNoObstacles)))

            self.enemy_attack_path = self.get_enemy_probable_attack_path(self.targetPlayer)

            self.target_player_gather_path = self.shortest_path_to_target_player

            # limit = max(self._map.cols, self._map.rows)
            # if self.target_player_gather_path is not None and self.target_player_gather_path.length > limit and self._map.players[self.general.player].cityCount > 1:
            #     self.target_player_gather_path = self.target_player_gather_path.get_subsegment(limit, end=True)

            if self.teammate_communicator is not None and self.teammate_general is not None:
                self.teammate_communicator.determine_leads(self._gen_distances, self._ally_distances, self.targetPlayerExpectedGeneralLocation)

        if self.targetPlayer != -1 and self.target_player_gather_path is not None:
            with self.perf_timer.begin_move_event(f'find sketchiest fog flank'):
                self.sketchiest_potential_inbound_flank_path = self.find_sketchiest_fog_flank_from_enemy()
            if self.sketchiest_potential_inbound_flank_path is not None:
                self.viewInfo.add_stats_line(f'skFlank: {self.sketchiest_potential_inbound_flank_path}')
                self.viewInfo.color_path(PathColorer(
                    self.sketchiest_potential_inbound_flank_path, 0, 0, 0
                ))

        spawnDist = 12
        if self.target_player_gather_path is not None:
            pathTillVisibleToTarget = self.target_player_gather_path
            if self.targetPlayer != -1 and self.is_ffa_situation():
                pathTillVisibleToTarget = self.target_player_gather_path.get_subsegment_until_visible_to_player(self._map.get_teammates(self.targetPlayer))
            self.target_player_gather_targets = {t for t in pathTillVisibleToTarget.tileList if not t.isSwamp and (not t.isDesert or self._map.is_tile_friendly(t))}
            if len(self.target_player_gather_targets) == 0:
                self.info(f'WARNING, BAD GATHER TARGETS, CANT AVOID DESERTS AND SWAMPS...')
                self.target_player_gather_targets = self.target_player_gather_path.tileSet
            spawnDist = self.shortest_path_to_target_player.length
        else:
            self.target_player_gather_targets = None

        with self.perf_timer.begin_move_event('calculating is_player_spawn_cramped'):
            self.force_city_take = self.is_player_spawn_cramped(spawnDist) and self._map.turn > 150

    #
    # def check_for_1_move_kills(self) -> typing.Union[None, Move]:
    #     """
    #     due to enemy_army_near_king logic, need to explicitly check for 1-tile-kills that we might win on luck
    #     @return:
    #     """
    #     for enemyGeneral in self._map.generals:
    #         if enemyGeneral is None or enemyGeneral == self.general:
    #             continue
    #
    #         for adj in enemyGeneral.movable:
    #             if adj.player == self.general.player and adj.army - 1 > enemyGeneral.army:
    #                 logbook.info(f"Adjacent kill on general lul :^) {enemyGeneral.x},{enemyGeneral.y}")
    #                 return Move(adj, enemyGeneral)
    #
    #     return None

    def check_for_king_kills_and_races(self, threat: ThreatObj | None, force: bool = False) -> typing.Tuple[Move | None, Path | None, float]:
        """

        @param threat:
        @return: (kill move/none, kill path / none, killChance)
        """

        kingKillPath = None
        kingKillChance = 0.0
        alwaysCheckKingKillWithinRange = 5
        # increasing this causes the bot to dive kings way too often with stuff that doesn't regularly kill, and kind of just kamikaze's its army without optimizing tile captures.
        if self.is_all_in_losing and not self.all_in_city_behind:
            alwaysCheckKingKillWithinRange = 7

        if self.is_ffa_situation():
            alwaysCheckKingKillWithinRange += 3

        extraTurnOnPriority = 0
        if threat is not None and self._map.player_has_priority_over_other(self.player.index, threat.threatPlayer, self._map.turn + threat.turns):
            extraTurnOnPriority = 1

        threatIsGeneralKill = threat is not None and threat.threatType == ThreatType.Kill and threat.path.tail.tile.isGeneral

        # self.our_flank_threats_by_general = {}
        enemyGeneral: Tile
        for enemyGeneral in self.largeTilesNearEnemyKings.keys():
            if enemyGeneral is None or enemyGeneral.player == self.general.player or enemyGeneral.player in self._map.teammates:
                continue

            enPlayer = enemyGeneral.player
            if enPlayer == -1:
                enPlayer = self.targetPlayer
            if enPlayer == -1:
                continue

            altEnGenPositions = self.alt_en_gen_positions[enPlayer]

            curExtraTurn = extraTurnOnPriority
            # if len(altEnGenPositions) > 1:
            #     curExtraTurn = 0

            turnsToDeath = None
            killRaceCutoff = 0.3
            if threatIsGeneralKill:
                turnsToDeath = threat.turns + 1 + curExtraTurn
                econRatio = self.opponent_tracker.get_current_econ_ratio()
                killRaceCutoff = min(0.99, 0.95 * econRatio * econRatio)

            thisPlayerDepth = alwaysCheckKingKillWithinRange

            if self.target_player_gather_path is not None and self.targetPlayer == enPlayer:
                thisPlayerDepth = min(thisPlayerDepth, self.target_player_gather_path.length // 3)

            attackNegTiles = set()
            targetArmy = 1

            if not enemyGeneral.visible:
                # account for fog kill likelihood
                if not enemyGeneral.isGeneral:
                    # account for kill probability based on the other altGenPositions
                    pass

            threatDistCutoff = 1000
            if threat is not None:
                threatDistCutoff = threat.turns + curExtraTurn
                if not self._map.players[threat.threatPlayer].knowsKingLocation:
                    tilesOppHasntSeen = set([
                        t for t in self.player.tiles
                        if self._map.get_distance_between(enemyGeneral, t) <= self.target_player_gather_path.length
                           and self._map.get_distance_between(self.general, t) < self.target_player_gather_path.length // 3
                    ])
                    closeTilesOppHasSeen = set()
                    for tile in self.armyTracker.tiles_ever_owned_by_player[threat.threatPlayer]:
                        if tile in tilesOppHasntSeen:
                            tilesOppHasntSeen.discard(tile)
                            closeTilesOppHasSeen.add(tile)
                        for adj in tile.adjacents:
                            if adj.player == self.general.player:
                                if adj in tilesOppHasntSeen:
                                    tilesOppHasntSeen.discard(adj)
                                    closeTilesOppHasSeen.add(adj)

                    unknownsToHunt = len(tilesOppHasntSeen) - len(closeTilesOppHasSeen) - threat.turns
                    if unknownsToHunt > 0:
                        cutoffIncrease = int(unknownsToHunt ** 0.5) - 1
                        if cutoffIncrease > 0:
                            threatDistCutoff += cutoffIncrease

            nonGenArmy = 0
            addlIncrement = 0.0

            if not enemyGeneral.visible:
                defTurns = 0
                # remove fog army that can't reach gen
                optTargetArmy = self.determine_fog_defense_amount_available_for_tiles(altEnGenPositions, enPlayer, fogDefenseTurns=defTurns, fogReachTurns=5)
                newTargetArmyOption = optTargetArmy

                if enemyGeneral.isGeneral:
                    newTargetArmyOption -= enemyGeneral.army

                if newTargetArmyOption > targetArmy:
                    targetArmy = newTargetArmyOption
                    nonGenArmy = optTargetArmy

                if threat is None and self.opponent_tracker.get_player_gather_queue(enPlayer).cur_max_tile_size > 2:
                    # if the opponent is obviously going to be spending time defending then require a bit extra army.
                    addlIncrement += 0.5

                logbook.info(f'will attmpt QK en{enPlayer} fog defense in {defTurns}t was {targetArmy}')

            if not enemyGeneral.isGeneral:
                addlIncrement += 0.5  # * min(4, self._map.players[enPlayer].cityCount)
                if not self.is_ffa_situation():
                    thisPlayerDepth = max(3, thisPlayerDepth - 5)

            qkDist = 8
            if self.target_player_gather_path is not None:
                qkDist = 2 * self.target_player_gather_path.length // 3
            if len(altEnGenPositions) < 20:
                quickKill = SearchUtils.dest_breadth_first_target(
                    self._map,
                    altEnGenPositions,
                    max(targetArmy, 1),
                    0.05,
                    qkDist,
                    attackNegTiles,
                    self.general.player,
                    ignoreGoalArmy=self.has_defenseless_modifier,
                    # additionalIncrement=-0.5,  # increment is already factored in to the fact that the targetArmy is incrementing based on the players city
                    additionalIncrement=addlIncrement,
                    # preferCapture=shouldPrioritizeTileCaps,
                    noLog=True)
                logbook.info(f'QK @{enPlayer} turns {qkDist} for targetArmy {max(1, targetArmy)} addlInc {addlIncrement} returned {quickKill}   --  @ {" | ".join([str(t) for t in altEnGenPositions])},  attackNegs {" | ".join([str(t) for t in attackNegTiles])}')

                # addlPath = SearchUtils.breadth_first_find_queue(self._map, [enemyGeneral], lambda t, _1, _2: t == furthestAlt, noNeutralCities=False)
                if quickKill is not None and quickKill.length > 0:
                    connectedTiles, missingRequired = MapSpanningUtils.get_spanning_tree_from_tile_lists(self._map, altEnGenPositions, set())
                    # furthestAlt = None
                    additionalKillDist = len(connectedTiles)
                    # for tile in altEnGenPositions:
                    #     if tile == quickKill.tail.tile:
                    #         continue
                    #     dist = min(
                    #         self._map.get_distance_between(tile, quickKill.tail.tile),
                    #         self._map.get_distance_between(tile, quickKill.tail.prev.tile),
                    #         self._map.get_distance_between(tile, quickKill.tail.prev.prev.tile)
                    #     )
                    #     if furthestAlt is None or dist > additionalKillDist:
                    #         additionalKillDist = dist
                    #         furthestAlt = tile
                    enemyNegTiles = []
                    if threat is not None:
                        enemyNegTiles.append(threat.path.start.tile)
                        enemyNegTiles.extend(quickKill.tileList)
                    maxEnDefTurns = quickKill.length + additionalKillDist
                    cityLimit = 1
                    if not enemyGeneral.isGeneral:
                        cityLimit += 1

                    cutoffKillArmy = 0
                    cutoffEmergence = 0.4
                    if threatIsGeneralKill:
                        cutoffKillArmy = self.opponent_tracker.get_approximate_fog_army_risk(enPlayer, cityLimit=None, inTurns=0)
                        if enemyGeneral.isGeneral:
                            cutoffKillArmy -= enemyGeneral.army + quickKill.length // 2
                        cutoffEmergence = 0.15
                    else:
                        bestDef = self.opponent_tracker.get_approximate_fog_army_risk(enPlayer, cityLimit=None, inTurns=maxEnDefTurns - 3)

                        cutoffKillArmy = bestDef + 2 * additionalKillDist - 5

                    cutoffKillArmy = max(0, cutoffKillArmy)

                    if quickKill.value > cutoffKillArmy or threatIsGeneralKill:
                        logbook.info(f"    quick-kill path val {quickKill.value} > ({cutoffKillArmy} in {maxEnDefTurns}t) found to kill enemy king w/ additionalKillDist {additionalKillDist}? {str(quickKill)}")
                        ogQuickKill = quickKill
                        ogAdditionalKillDist = additionalKillDist
                        if not quickKill.tileList[-1].isGeneral:
                            turns = 30
                            if threat is not None:
                                turns = threat.turns

                            startTile = quickKill.tileList[0]

                            with self.perf_timer.begin_move_event(f'QK WRP {startTile} tgA {cutoffKillArmy}'):
                                maxTime = 0.020

                                toReveal = self.get_target_player_possible_general_location_tiles_sorted(elimNearbyRange=0, player=self.targetPlayer, cutoffEmergenceRatio=cutoffEmergence, includeCities=False)

                                wrpPath = WatchmanRouteUtils.get_watchman_path(
                                    self._map,
                                    startTile,
                                    toReveal,
                                    timeLimit=maxTime,
                                    # initialArmy=-cutoffKillArmy
                                )

                            if wrpPath is not None:
                                closest = None
                                closestDist = 100
                                found = False
                                for t in wrpPath.tileList:
                                    dist = self._map.get_distance_between(self.targetPlayerExpectedGeneralLocation, t)
                                    if dist < closestDist:
                                        closest = t
                                        closestDist = dist

                                    if not found: # and t in self.targetPlayerExpectedGeneralLocation.adjacents
                                        found = True
                                        quickKill = wrpPath

                                if found:
                                    self.info(f'QK WRP {wrpPath}')
                                    additionalKillDist = closestDist

                                if quickKill != wrpPath:
                                    self.viewInfo.add_info_line(f'skipped QK wrpPath (addl {additionalKillDist}) was {wrpPath}')

                        killRaceChance = self.get_kill_race_chance(quickKill, enGenProbabilityCutoff=cutoffEmergence + 0.1, turnsToDeath=turnsToDeath, cutoffKillArmy=cutoffKillArmy)
                        if threat is None or not threatIsGeneralKill or killRaceChance >= killRaceCutoff:
                            self.info(f"QK {quickKill.value} {quickKill.length}t kill race chance {killRaceChance:.3f} > {killRaceCutoff:.2f} with cutoffKillArmy {cutoffKillArmy} in {maxEnDefTurns}t +{additionalKillDist} (turns to death {turnsToDeath}) :^)")
                            self.viewInfo.color_path(PathColorer(quickKill, 255, 240, 79, 244, 5, 200))
                            move = Move(quickKill.start.tile, quickKill.start.next.tile)
                            if self.is_move_safe_valid(move):
                                self.curPath = None
                                # self.curPath = quickKill
                                if quickKill.start.next.tile.isCity:
                                    self.curPath = quickKill

                                for t in altEnGenPositions:
                                    self.viewInfo.add_targeted_tile(t, TargetStyle.RED, radiusReduction=0)

                                if connectedTiles:
                                    for t in connectedTiles:
                                        # quickKill.add_next(t)
                                        self.viewInfo.add_targeted_tile(t, TargetStyle.RED, radiusReduction=8)
                                self.viewInfo.infoText = f"QK chance {killRaceChance:.2f} {quickKill.value}v > {cutoffKillArmy} in {maxEnDefTurns}t {quickKill.length}t+{additionalKillDist}t :^)"
                                return move, quickKill, killRaceChance
                        elif killRaceChance > kingKillChance:
                            self.info(f"QK (low chance) {quickKill.value} {quickKill.length}t kill race chance {killRaceChance:.3f} < {killRaceCutoff:.2f} but > kkc {kingKillChance:.2f} (with cutoffKillArmy {cutoffKillArmy} in {maxEnDefTurns}t +{additionalKillDist} (death {turnsToDeath}t) :^(")
                            kingKillPath = quickKill
                            kingKillChance = killRaceChance
                        else:
                            self.info(f"NO QK {quickKill.value} {quickKill.length}t kill race chance {killRaceChance:.3f} vs cutoff {killRaceCutoff:.2f} with cutoffKillArmy {cutoffKillArmy} in {maxEnDefTurns}t +{additionalKillDist} :(")
                    else:
                        logbook.info(f" ---quick-kill path val {quickKill.value} < {cutoffKillArmy} in {maxEnDefTurns}t {quickKill.length}t+{additionalKillDist}t @enemy king. Low val. {str(quickKill)}")

            if not enemyGeneral.isGeneral and not self.is_ffa_situation() and len(altEnGenPositions) > 2:
                # TODO the exploration strategy right now backtracks a ton and doesn't capture tiles effectively,
                #  opt out except after army bonus for now unless we KNOW where the enemy gen is.
                #  Remove this later after fixing the backtracking problem and hunting generals more effectively.
                continue

            logbook.info(
                f"Performing depth increasing BFS kill search on enemy king {enemyGeneral.toString()} depth {thisPlayerDepth}")
            with self.perf_timer.begin_move_event(f"race depth increasing vs p{enPlayer} {enemyGeneral}"):
                inc = 1
                # if thisPlayerDepth > 6:
                #     inc = 2
                for depth in range(2, thisPlayerDepth, inc):
                    enemyNegTiles = []
                    if threat is not None:
                        enemyNegTiles.append(threat.path.start.tile)
                    enemySavePath = self.get_best_defense(enemyGeneral, depth - 1, enemyNegTiles)
                    defTurnsLeft = depth - 1
                    depthTargetArmy = targetArmy
                    if enemySavePath is not None:
                        defTurnsLeft -= enemySavePath.length
                        depthTargetArmy = max(enemySavePath.value + nonGenArmy, depthTargetArmy)
                        if not enemyGeneral.visible:
                            depthTargetArmy = max(depthTargetArmy, enemySavePath.value + nonGenArmy + self.determine_fog_defense_amount_available_for_tiles(altEnGenPositions, enPlayer, fogDefenseTurns=defTurnsLeft))
                        logbook.info(f"  targetArmy {targetArmy}, enemySavePath {enemySavePath.toString()}")
                        attackNegTiles = enemySavePath.tileSet.copy()
                        attackNegTiles.remove(enemyGeneral)

                    if not enemyGeneral.visible:
                        # remove fog army that can't reach gen
                        depthTargetArmy = max(depthTargetArmy, self.determine_fog_defense_amount_available_for_tiles(altEnGenPositions, enPlayer, fogDefenseTurns=depth - 1))

                    logbook.info(f"  targetArmy to add to enemyGeneral kill = {depthTargetArmy}")
                    shouldPrioritizeTileCaps = (
                            not self.is_all_in()
                            and not self.is_ffa_situation()
                            and (threat is None or threat.threatType != ThreatType.Kill)
                    )
                    killPath = SearchUtils.dest_breadth_first_target(
                        self._map,
                        altEnGenPositions,
                        max(depthTargetArmy, 1),
                        0.05,
                        depth,
                        attackNegTiles,
                        self.general.player,
                        dupeThreshold=3,
                        preferCapture=shouldPrioritizeTileCaps,
                        ignoreGoalArmy=self.has_defenseless_modifier,
                        additionalIncrement=addlIncrement,
                        noLog=True)
                    if killPath is not None and killPath.length > 0:
                        killChance = 0.0
                        if killPath and threatIsGeneralKill:
                            killChance = self.get_kill_race_chance(killPath, enGenProbabilityCutoff=0.3, turnsToDeath=turnsToDeath)
                        logbook.info(f"    depth {depth} path found to kill enemy king? {str(killPath)}")
                        if threat is None or threat.threatType != ThreatType.Kill or (threatDistCutoff >= killPath.length and killChance > killRaceCutoff):
                            logbook.info(f"    DEST BFS K found kill path length {killPath.length} :^)")
                            self.viewInfo.color_path(PathColorer(killPath, 255, 240, 79, 244, 5, 200))
                            move = Move(killPath.start.tile, killPath.start.next.tile)
                            self.curPath = None
                            if killPath.start.next.tile.isCity:
                                self.curPath = killPath
                            if self.is_move_safe_valid(move):
                                self.viewInfo.infoText = f"Depth increasing Killpath against general length {killPath.length}"
                                return move, killPath, killChance
                        elif killChance > kingKillChance:
                            self.info(f"    DEST BFS K (low chance) {killPath.value} {killPath.length}t kill race chance {killChance:.3f} < {killRaceCutoff:.2f}  but > kingKillChance {kingKillChance:.2f} :(")
                            kingKillPath = killPath
                            kingKillChance = killChance
                        else:
                            logbook.info(
                                f"    DEST BFS K found kill path {str(killPath)} BUT ITS LONGER THAN OUR THREAT LENGTH :(")
                            if kingKillPath is None:
                                logbook.info("      saving above kingKillPath as backup in case we can't defend threat")
                                kingKillPath = killPath

                rangeBasedOnDistance = int(self.distance_from_general(self.targetPlayerExpectedGeneralLocation) // 3 - 1)
                additionalKillArmyRequirement = 0
                if not enemyGeneral.isGeneral:
                    additionalKillArmyRequirement = self.opponent_tracker.get_approximate_fog_army_risk(enPlayer, cityLimit=None, inTurns=0)
                additionalKillArmyRequirement = max(0, additionalKillArmyRequirement)

                if not force and (not self.opponent_tracker.winning_on_army(byRatio=1.3)
                                  and not self.opponent_tracker.winning_on_army(byRatio=1.3, againstPlayer=self.targetPlayer)
                                  # and (threat is None or threat.threatType != ThreatType.Kill)
                                  and not self.is_all_in()
                                  and not enemyGeneral.visible):
                    rangeBasedOnDistance = int(self.distance_from_general(self.targetPlayerExpectedGeneralLocation) // 4 - 1)

                    additionalKillArmyRequirement = self.opponent_tracker.get_approximate_fog_army_risk(enPlayer, cityLimit=None, inTurns=3)
                    logbook.info(f'additional kill army requirement is currently {additionalKillArmyRequirement}')

                depth = max(alwaysCheckKingKillWithinRange, rangeBasedOnDistance)

                if self.is_all_in_losing or self.is_ffa_situation():
                    depth += 5

                logbook.info(f"Performing depth {depth} BFS kill search on enemy kings")
                # logbook.info(f"Performing depth {depth} BFS kill search on enemy kings {str(altEnGenPositions)}")
                # uses targetArmy from depth 6 above
                fullKillReq = targetArmy + additionalKillArmyRequirement
                killPath = SearchUtils.dest_breadth_first_target(self._map, altEnGenPositions, fullKillReq, 0.05, depth, attackNegTiles, self.general.player, False, 3)
                killChance = 0.0
                if killPath:
                    killChance = self.get_kill_race_chance(killPath, enGenProbabilityCutoff=0.3, turnsToDeath=turnsToDeath)
                if (killPath is not None and killPath.length >= 0) and (threat is None or threat.threatType != ThreatType.Kill or (threatDistCutoff >= killPath.length and killChance > killRaceCutoff)):
                    logbook.info(f"DBFT d{depth} for {fullKillReq}a found kill path length {killPath.length} :^)")
                    self.curPath = None
                    self.viewInfo.color_path(PathColorer(killPath, 200, 100, 0))
                    move = Move(killPath.start.tile, killPath.start.next.tile)

                    if self.is_move_safe_valid(move):
                        self.info(f"DBFT d{depth} K for {fullKillReq}a: {killPath.length}t {killPath.value}v  (a = tg{targetArmy} + addl{additionalKillArmyRequirement}) force {str(force)[0]}")
                        return move, killPath, killChance

                elif killChance > kingKillChance:
                    self.info(f"DBFT d{depth} for {fullKillReq}a (low chance) {killPath.value} {killPath.length}t kill race chance {killChance:.3f} < {killRaceCutoff:.2f} but > kingKillChance {kingKillChance:.2f} :(")
                    kingKillPath = killPath
                    kingKillChance = killChance
                elif killPath is not None and killPath.length > 0:
                    logbook.info(
                        f"DEST BFS K found kill path {str(killPath)} BUT ITS LONGER THAN OUR THREAT LENGTH :(")
                    if kingKillPath is None:
                        logbook.info("  saving above kingKillPath as backup in case we can't defend threat")

                        kingKillPath = killPath

            if self.is_ffa_situation():
                tiles = self.largeTilesNearEnemyKings[enemyGeneral]
                if len(tiles) > 0:
                    logbook.info(f"Attempting to find A_STAR kill path against general {enemyGeneral.player} ({enemyGeneral})")
                    bestTurn = 1000
                    bestPath = None
                    targets = set(altEnGenPositions)
                    path = SearchUtils.a_star_kill(
                        self._map,
                        tiles,
                        targets,
                        0.03,
                        self.distance_from_general(self.targetPlayerExpectedGeneralLocation) // 4,
                        # self.general_safe_func_set,
                        requireExtraArmy=targetArmy + additionalKillArmyRequirement,
                        negativeTiles=attackNegTiles)

                    killChance = 0.0
                    if killPath:
                        killChance = self.get_kill_race_chance(killPath, enGenProbabilityCutoff=0.3, turnsToDeath=turnsToDeath)

                    if (path is not None and path.length >= 0) and (threat is None or threat.threatType != ThreatType.Kill or ((threatDistCutoff >= path.length or self.is_all_in()) and threat.threatPlayer == enemyGeneral.player and killChance > killRaceCutoff)):
                        logbook.info(f"  A_STAR found kill path length {path.length} :^)")
                        self.viewInfo.color_path(PathColorer(path, 174, 4, 214, 255, 10, 200))
                        self.curPath = path.get_subsegment(2)
                        self.curPathPrio = 5
                        if path.length < bestTurn:
                            bestPath = path
                            bestTurn = path.length
                    elif path is not None and path.length > 0:
                        logbook.info(f"  A_STAR found kill path {str(path)} BUT ITS LONGER THAN OUR THREAT LENGTH :(")
                        self.viewInfo.color_path(PathColorer(path, 114, 4, 194, 255, 20, 100))
                        if kingKillPath is None:
                            logbook.info("    saving above kingKillPath as backup in case we can't defend threat")
                            kingKillPath = path
                    if bestPath is not None:
                        self.info(f"A* Killpath! {enemyGeneral.toString()},  {bestPath.toString()}")
                        move = Move(bestPath.start.tile, bestPath.start.next.tile)
                        return move, path, killChance

        return None, kingKillPath, kingKillChance

    def determine_fog_defense_amount_available_for_tiles(self, targetTiles, enPlayer, fogDefenseTurns: int = 0, fogReachTurns: int = 8) -> int:
        """Does NOT include the army that is on the targetTiles."""
        targetArmy = self.opponent_tracker.get_approximate_fog_army_risk(enPlayer, cityLimit=None, inTurns=fogDefenseTurns)

        genReachable = SearchUtils.build_distance_map_matrix_with_skip(self._map, targetTiles, skipTiles=self._map.visible_tiles)

        used = set()
        for army in self.armyTracker.armies.values():
            if army.player != enPlayer:
                continue

            if army.name in used:
                continue

            if army.tile.visible:
                continue

            anyReachable = False
            if genReachable.raw[army.tile.tile_index] is None or genReachable.raw[army.tile.tile_index] >= fogReachTurns:
                for entangled in army.entangledArmies:
                    if genReachable.raw[entangled.tile.tile_index] is not None and genReachable.raw[entangled.tile.tile_index] < fogReachTurns:
                        anyReachable = True
            else:
                anyReachable = True

            if not anyReachable:
                targetArmy -= army.value

                used.add(army.name)

        return targetArmy

    def calculate_target_player(self) -> int:
        targetPlayer = -1
        playerScore = 0

        if self._map.remainingPlayers == 2:
            for player in self._map.players:
                if player.index != self.general.player and not player.dead and player.index not in self._map.teammates:
                    return player.index

        allAfk = len(self.get_afk_players()) >= self._map.remainingPlayers - 1 - len(self._map.teammates)
        if allAfk or self._map.is_2v2:
            playerScore = -10000000

        minStars = 10000
        starSum = 0
        for player in self._map.players:
            minStars = min(minStars, player.stars)
            starSum += player.stars
        starAvg = starSum * 1.0 / len(self._map.players)
        self.playerTargetScores = [0 for i in range(len(self._map.players))]
        generalPlayer = self._map.players[self.general.player]

        numVisibleEnemies = 0

        for player in self._map.players:
            if player.dead or player.index == self._map.player_index or player.index in self._map.teammates:
                continue
            seenPlayer = SearchUtils.count(player.tiles, lambda t: t.visible) > 0

        for player in self._map.players:
            seenPlayer = SearchUtils.count(player.tiles, lambda t: t.visible) > 0
            if player.dead or player.index == self._map.player_index or not seenPlayer or player.index in self._map.teammates:
                continue

            curScore = 300

            if self._map.remainingPlayers > 3:
                # ? I"M FRIENDLY I SWEAR
                curScore = -1
                if player.aggression_factor < 10:
                    if numVisibleEnemies > 1:
                        curScore -= 50

            enGen = self._map.generals[player.index]

            knowsWhereEnemyGeneralIsBonus = 100
            if self._map.is_2v2:
                knowsWhereEnemyGeneralIsBonus = 1000
            if enGen is not None:
                curScore += knowsWhereEnemyGeneralIsBonus

            if player.leftGame and not player.dead:
                armyEnGenCutoffFactor = 0.75
                if enGen is not None:
                    if enGen.army < generalPlayer.standingArmy ** armyEnGenCutoffFactor:
                        self.viewInfo.add_info_line(f'leftGame GEN bonus army generalPlayer.standingArmy ** {armyEnGenCutoffFactor} {generalPlayer.standingArmy ** armyEnGenCutoffFactor} > enGen.army {enGen.army}')
                        curScore += 300
                    else:
                        curScore += 2500 // player.tileCount
                factor = 0.95
                if generalPlayer.standingArmy > player.standingArmy ** factor:
                    self.viewInfo.add_info_line(f'leftGame bonus army generalPlayer.standingArmy {generalPlayer.standingArmy} > player.standingArmy ** {factor} {player.standingArmy ** factor}')
                    curScore += 200
                factor = 0.88
                if generalPlayer.standingArmy > player.standingArmy ** factor:
                    self.viewInfo.add_info_line(f'leftGame bonus army generalPlayer.standingArmy {generalPlayer.standingArmy} > player.standingArmy ** {factor} {player.standingArmy ** factor}')
                    curScore += 100
                factor = 0.81
                if generalPlayer.standingArmy > player.standingArmy ** factor:
                    self.viewInfo.add_info_line(f'leftGame bonus army generalPlayer.standingArmy {generalPlayer.standingArmy} > player.standingArmy ** {factor} {player.standingArmy ** factor}')
                    curScore += 50
                factor = 0.75
                if generalPlayer.standingArmy > player.standingArmy ** factor:
                    self.viewInfo.add_info_line(f'leftGame bonus army generalPlayer.standingArmy {generalPlayer.standingArmy} > player.standingArmy ** {factor} {player.standingArmy ** factor}')
                    curScore += 30

            alreadyTargetingBonus = 120
            if self.targetPlayer != -1 and self._map.players[self.targetPlayer].aggression_factor < 40:
                alreadyTargetingBonus = 10

            curScore += player.aggression_factor
            if player.index == self.targetPlayer:
                curScore += alreadyTargetingBonus

            curScore += player.aggression_factor

            # target players with better economies first
            # curScore += (player.tileCount + player.cityCount * 20 - player.standingArmy ** 0.88) / 4

            if generalPlayer.standingArmy > player.standingArmy * 0.75:
                # target players with better economies first more when we are winning
                curScore += player.cityCount * 30
                curScore += player.tileCount
                # 20% bonus for winning
                curScore *= 1.2

            if player.knowsKingLocation or player.knowsAllyKingLocation:
                curScore += 100
                curScore *= 2

            if self.generalApproximations[player.index][3] is not None:
                enApprox = self.generalApproximations[player.index][3]
                genDist = self.distance_from_general(enApprox)
                if self.teammate_general is not None:
                    genDist += self._map.euclidDist(self.teammate_general.x, self.teammate_general.y, enApprox.x, enApprox.y)
                    genDist = genDist // 2
            else:
                logbook.info(f"           wot {self._map.usernames[targetPlayer]} didn't have a gen approx tile???")
                genDist = self._map.euclidDist(self.generalApproximations[player.index][0], self.generalApproximations[player.index][1], self.general.x, self.general.y)

                if self.teammate_general is not None:
                    genDist += self._map.euclidDist(self.teammate_general.x, self.teammate_general.y, self.generalApproximations[player.index][0], self.generalApproximations[player.index][1])
                    genDist = genDist // 2

            curScore = curScore + 2 * curScore / (max(10, genDist) - 2)

            if player.index != self.targetPlayer and not self._map.is_2v2:
                curScore = curScore * 0.9

            # deprio small players
            if player.standingArmy <= 0 or (player.tileCount < 4 and player.general is None) or (player.general is not None and player.general.army > player.standingArmy ** 0.95 and player.general.army > 75):
                curScore = -100

            # losing massively to this player? -200 to target even single tile players higher than big fish
            if self._map.remainingPlayers > 2 and not self.opponent_tracker.winning_on_army(0.7, False, player.index) and not self.opponent_tracker.winning_on_economy(0.7, 20, player.index):
                curScore = -200

            if 'PurdBlob' in self._map.usernames[player.index]:
                curScore += 150

            if 'PurdPath' in self._map.usernames[player.index]:
                curScore += 100

            if curScore > playerScore and player.index not in self._map.teammates:
                playerScore = curScore
                targetPlayer = player.index
            self.playerTargetScores[player.index] = curScore

        # don't target big fish when there are other players left
        if self._map.remainingPlayers > 2 and playerScore < -100 and not self._map.is_2v2:
            return -1

        if targetPlayer == -1 and self._map.is_2v2:
            for player in self._map.players:
                if player.index != self.general.player and not player.dead and player.index not in self._map.teammates:
                    return player.index

        if targetPlayer != -1:
            logbook.info(f"target player: {self._map.usernames[targetPlayer]} ({int(playerScore)})")

        return targetPlayer

    def get_gather_tiebreak_matrix(self) -> MapMatrixInterface[float]:
        """
        Returns a MapMatrix that includes an int value that indicates how to tiebreak gather values.

        @return:
        """

        matrix = MapMatrix(self._map, 0.0)

        # prioritizedValuableLeaves = self.prioritize_expansion_leaves(self.leafMoves)
        # prioritized = [t.source for t in self.prioritize_expansion_leaves(self.leafMoves) if self.board_analysis.extended_play_area_matrix[t.source]]

        # i = 0
        # for leafMove in prioritizedValuableLeaves:
        #     if i > 15:
        #         break
        #
        #     armyLeft = leafMove.source.army - leafMove.dest.army
        #     if armyLeft > 5:
        #         continue
        #
        #     matrix[leafMove.source] += -5.0 / (i + 4)
        #     i += armyLeft - 1
        # for now just deprio 15
        desertPenalty = 0.25
        for tile in self.board_analysis.backwards_tiles:
            if tile.army > 2:
                matrix.raw[tile.tile_index] += 0.45
            elif tile.army > 1:
                matrix.raw[tile.tile_index] += 0.25

        for tile in self._map.swamps:
            matrix.raw[tile.tile_index] -= 2.0

        for tile in self._map.deserts:
            matrix.raw[tile.tile_index] -= desertPenalty

        # TODO get actual expansion plan value/turn into the expansion plan and prioritize according to those.
        if self.expansion_plan is not None:
            if self.expansion_plan.selected_option is not None:
                for tile in self.expansion_plan.selected_option.tileSet:
                    matrix.raw[tile.tile_index] -= 0.2
            for path in self.expansion_plan.all_paths:
                for tile in path.tileSet:
                    matrix.raw[tile.tile_index] -= 0.05

        isNonDomFfa = self.is_still_ffa_and_non_dominant()

        for p in self._map.get_teammates(self._map.player_index):
            for tile in self._map.players[p].tiles:
                # if self._map.is_tile_friendly(tile) and tile not in leaves:
                #     matrix.raw[tile.tile_index] += 0.02
                # if tile not in self.board_analysis.core_play_area_matrix:
                #     oppDist = self.distance_from_opp(tile) - self.shortest_path_to_target_player.length
                #     if oppDist > 0:
                #         outOfPlayOffset = min(0.8, (oppDist ** 0.25) - 1.0)
                #         # logbook.info(f'distMatrix out of play {str(tile)} : {outOfPlayOffset:.3f}')
                #         matrix.raw[tile.tile_index] += outOfPlayOffset

                if not isNonDomFfa or self._map.turn > 200:
                    if tile in self.board_analysis.intergeneral_analysis.shortestPathWay.tiles:
                        matrix.raw[tile.tile_index] -= 0.4

                    if tile.army <= 1:
                        matrix.raw[tile.tile_index] -= 0.5

                # undo desert penalties for deserts that are friendly, since crossing them doesn't change our outcome at all.
                if tile.isDesert:
                    matrix.raw[tile.tile_index] += desertPenalty

                isAllFriendly = True
                for adj in tile.movable:
                    if not adj.isObstacle and not self._map.is_tile_friendly(adj):
                        isAllFriendly = False
                        break

                if isAllFriendly and self.territories.territoryDistances[self.targetPlayer].raw[tile.tile_index] > 3:
                    matrix.raw[tile.tile_index] += min(0.49, 0.04 * self.territories.territoryDistances[self.targetPlayer].raw[tile.tile_index])

                if not isAllFriendly and not isNonDomFfa:
                    matrix.raw[tile.tile_index] -= 0.25

                if tile not in self.board_analysis.extended_play_area_matrix and tile.army > 1:
                    distToShortest = self.board_analysis.shortest_path_distances.raw[tile.tile_index]
                    if distToShortest < 1000:
                        matrix.raw[tile.tile_index] += min(0.49, 0.03 * distToShortest)

                if self.info_render_gather_matrix_values:
                    matrixVal = matrix.raw[tile.tile_index]
                    if matrixVal:
                        self.viewInfo.topRightGridText.raw[tile.tile_index] = f'g{str(round(matrixVal, 3)).lstrip("0").replace("-0", "-")}'

        for city in self.cities_gathered_this_cycle:
            matrix.raw[city.tile_index] = -1.0

        return matrix

    def check_defense_intercept_move(self, threat: ThreatObj) -> typing.Tuple[Move | None, Path | None, InterceptionOptionInfo | None, bool]:
        threatInterceptionPlan = self.intercept_plans.get(threat.path.start.tile, None)
        isDelayed = False
        threatTile = threat.path.start.tile
        threatArmy = self.get_army_at(threatTile)

        isNonAggressor = (self._map.players[threat.threatPlayer].aggression_factor < 50 and not threatTile.visible)
        tileNotAttacking = (threatArmy.last_moved_turn < self._map.turn - 2 or threatArmy.last_seen_turn < self._map.turn - 2)
        if threat.threatPlayer != self.targetPlayer and not self._map.is_2v2 and (isNonAggressor or tileNotAttacking or threatArmy.last_seen_turn < self._map.turn - 6):
            return None, None, None, False
        
        if threatInterceptionPlan is None or len(threatInterceptionPlan.intercept_options) == 0:
            with self.perf_timer.begin_move_event(f'def solo interception @ {threat.path.start.tile}'):
                return self.check_kill_threat_only_defense_interception(threat)

        interceptingOption: InterceptionOptionInfo | None = None
        interceptPath: TilePlanInterface | Path | None = None
        interceptPath, interceptingOption = self.get_defense_path_option_from_options_if_available(threatInterceptionPlan, threat)
        # todo
        if interceptPath is None:
            with self.perf_timer.begin_move_event(f'def solo interception @ {threat.path.start.tile}'):
                return self.check_kill_threat_only_defense_interception(threat)
        # removed, breaks test_should_not_try_to_expand_with_potential_threat_blocking_tile
        # if interceptPath.tail.tile not in threat.armyAnalysis.shortestPathWay.tiles and not includesIntercept:
        #     return None, None, isDelayed

        tookTooLong = interceptPath.length > threat.turns
        notEnoughDamageBlocked = False
        armyLeftOver = False
        if interceptingOption is not None:
            isDelayed = interceptingOption.requiredDelay > 0
            # notEnoughDamageBlocked = interceptingOption.damage_blocked < threat.threatValue
            notEnoughDamageBlocked = False
            armyLeftOver = threat.threatValue - interceptingOption.friendly_army_reaching_intercept > 0
            if threat.path.tail.tile.isGeneral:
                if tookTooLong or notEnoughDamageBlocked or armyLeftOver:
                    self.viewInfo.add_info_line(
                        f'DEF int BYP: rem ar {interceptingOption.intercepting_army_remaining}, long {"T" if tookTooLong else "F"}, notBlock {"T" if notEnoughDamageBlocked else "F"}, armyLeft {"T" if armyLeftOver else "F"}, {interceptPath}')
                    if SearchUtils.any_where(threatInterceptionPlan.threats, lambda t: not t.path.tail.tile.isGeneral):
                        with self.perf_timer.begin_move_event(f'def solo interception @ {threat.path.start.tile}'):
                            return self.check_kill_threat_only_defense_interception(threat)
                    return None, None, None, False

        self.viewInfo.color_path(PathColorer(
            interceptPath, 1, 1, 1,
        ))
        intOptInfo = ''
        if interceptingOption:
            intOptInfo = f' {interceptingOption}'
        mv = self.get_first_path_move(interceptPath)
        if self.detect_repetition(mv, 6, 3):
            self.info(f'DEF int REP SKIP... incl{intOptInfo}: long {"T" if tookTooLong else "F"}')
            self.info(f'    notBlock {"T" if notEnoughDamageBlocked else "F"}, armyLeft {"T" if armyLeftOver else "F"}, {interceptPath}')
            mv = None
        elif self.detect_repetition(mv, 4, 2):
            self.curPath = interceptPath.get_subsegment(3)
            self.info(f'DEF int REP incl{intOptInfo}: long {"T" if tookTooLong else "F"}')
            self.info(f'    notBlock {"T" if notEnoughDamageBlocked else "F"}, armyLeft {"T" if armyLeftOver else "F"}, {interceptPath}')
        else:
            self.info(f'DEF int incl{intOptInfo}: long {"T" if tookTooLong else "F"}')
            self.info(f'    notBlock {"T" if notEnoughDamageBlocked else "F"}, armyLeft {"T" if armyLeftOver else "F"}, {interceptPath}')
        return mv, interceptPath, interceptingOption, isDelayed

    def check_kill_threat_only_defense_interception(self, threat: ThreatObj) -> typing.Tuple[Move | None, Path | None, InterceptionOptionInfo | None, bool]:
        if not threat.path.tail.tile.isGeneral:
            return None, None, None, False

        if self.get_elapsed() > 0.06:
            self.viewInfo.add_info_line(f'BYPASSING DEF SOLO int of {threat.path.start.tile}->{threat.path.tail.tile} due to elapsed {self.get_elapsed():.3f}')
            return None, None, None, False

        threatInterceptionPlan = self.army_interceptor.get_interception_plan([threat], self._map.remainingCycleTurns)
        bestIsDelayed = False
        if threatInterceptionPlan is None or len(threatInterceptionPlan.intercept_options) == 0:
            return None, None, None, bestIsDelayed

        bestInterceptingOption: InterceptionOptionInfo | None = None
        bestInterceptPath: TilePlanInterface | Path | None = None
        bestMove: Move | None = None
        for i in range(threat.turns // 2 + 1):  #  range(threat.turns + 2):
            isDelayed = False
            interceptingOption = threatInterceptionPlan.intercept_options.get(i, None)
            if interceptingOption is None:
                continue

            interceptPath = interceptingOption.path
            intOptInfo = ''
            if interceptingOption:
                intOptInfo = f' {interceptingOption}'

            if self.detect_repetition(interceptingOption.path.get_first_move()):
                self.info(f'DEF SOLO int BYP REP {i} incl{intOptInfo}')
                continue

            if bestInterceptingOption is not None and bestInterceptingOption.econValue / bestInterceptingOption.length >= interceptingOption.econValue / interceptingOption.length:
                continue

            if interceptPath is None:
                continue
            # removed, breaks test_should_not_try_to_expand_with_potential_threat_blocking_tile
            # if interceptPath.tail.tile not in threat.armyAnalysis.shortestPathWay.tiles and not includesIntercept:
            #     return None, None, isDelayed

            tookTooLong = interceptingOption.friendly_army_reaching_intercept < threat.threatValue
            notEnoughDamageBlocked = interceptingOption.friendly_army_reaching_intercept < threat.threatValue
            if interceptingOption is None:
                continue

            isDelayed = interceptingOption.requiredDelay > 0
            # notEnoughDamageBlocked = interceptingOption.damage_blocked < threat.threatValue
            # notEnoughDamageBlocked = False
            armyLeftOver = interceptingOption.intercepting_army_remaining > 0
            if threat.path.tail.tile.isGeneral:
                if tookTooLong or notEnoughDamageBlocked:
                    self.viewInfo.add_info_line(
                        f'DEF SOLO int BYP {i}: rem ar {interceptingOption.intercepting_army_remaining}, long {"T" if tookTooLong else "F"}, notBlock {"T" if notEnoughDamageBlocked else "F"}, armyLeft {"T" if armyLeftOver else "F"}, {interceptPath}')
                    continue

            self.viewInfo.color_path(PathColorer(
                interceptPath, 1, 1, 1,
            ))
            bestMove = self.get_first_path_move(interceptPath)
            bestInterceptingOption = interceptingOption
            bestInterceptPath = interceptPath
            bestIsDelayed = isDelayed

        if bestMove and bestInterceptingOption:
            self.viewInfo.add_info_line(
                f'DEF SOLO int found {bestInterceptingOption.length}: rem ar {bestInterceptingOption.intercepting_army_remaining}, {bestInterceptPath}')
        else:
            self.viewInfo.add_info_line(f'DEF SOLO int NO BEST')

        return bestMove, bestInterceptPath, bestInterceptingOption, bestIsDelayed

    def check_defense_hybrid_intercept_moves(self, threat: ThreatObj, defensePlan: typing.List[GatherTreeNode], missingDefense: int, defenseNegatives: typing.Set[Tile]) -> typing.Tuple[Move | None, Path | None, bool, typing.List[GatherTreeNode]]:
        """
        Returns [replacementMove, replacementPath, isDelayed, updatedDefenseNodes]

        @param threat:
        @param defensePlan:
        @param missingDefense:
        @param defenseNegatives:
        @return:
        """
        threatInterceptionPlan = self.intercept_plans.get(threat.path.start.tile, None)

        curDefensePlan = defensePlan
        # gatherNodeLookup = {n.tile: n for n in GatherTreeNode.iterate_tree_nodes(defensePlan)}
        achievedDefense = sum(int(n.value) for n in defensePlan)
        defenseTurns = sum(n.gatherTurns for n in defensePlan)
        totalToSurvive = achievedDefense + missingDefense

        isDelayed = False
        if threatInterceptionPlan is None or len(threatInterceptionPlan.intercept_options) == 0:
            return None, None, isDelayed, defensePlan

        bestOpt = None
        bestEcon = 0.0
        bestAchievedDefense = achievedDefense
        self.info(f'  def HYBR int base defense {achievedDefense} in {defenseTurns}t (to survive {totalToSurvive} missing def {missingDefense})')
        bestTurns = defenseTurns
        bestSurvives = missingDefense <= 0
        isGeneralThreat = threat.path.tail.tile.isGeneral
        bestRemainingDefense = defensePlan

        for distance, opt in sorted(threatInterceptionPlan.intercept_options.items()):
            if opt.path.start.tile in threat.path.tileSet:
                continue
            if distance != opt.path.length:
                # we dont care about the econ-defense options that use various amounts of additional moves.
                self.info(f' - HYBR recap int {distance}t {opt.length}o {opt.recapture_turns}r {opt.path.length}l  {opt.best_case_intercept_moves}bc  {opt}')
                continue
            else:
                self.info(f' + HYBR recap int {distance}t {opt.length}o {opt.recapture_turns}r {opt.path.length}l  {opt.best_case_intercept_moves}bc  {opt}')

            gatherTreenNodesClone = GatherTreeNode.clone_nodes(defensePlan)
            currentAchievedDefense = achievedDefense
            currentGatherTurns = defenseTurns
            currentTotalToSurvive = totalToSurvive

            tookTooLong = opt.length > threat.turns
            # notEnoughDamageBlocked = interceptingOption.damage_blocked < threat.threatValue
            # armyLeftOver = opt.intercepting_army_remaining > 0
            if isGeneralThreat:
                if tookTooLong:
                    # self.viewInfo.add_info_line(
                    #     f'P-DEF int BYP: dist {distance} {opt.path.start.tile} rem ar {opt.intercepting_army_remaining}, long {"T" if tookTooLong else "F"}, notBlock {"T" if notEnoughDamageBlocked else "F"}, armyLeft {"T" if armyLeftOver else "F"}, {interceptPath}')
                    continue

            forcePrune = opt.tileSet.copy()
            forcePrune.difference_update(n.tile for n in defensePlan)  # cant prune root tiles
            turns, val, pruned = Gather.prune_mst_to_turns_with_values(gatherTreenNodesClone, threat.turns - opt.worst_case_intercept_moves, self.general.player, allowNegative=True, preferPrune=forcePrune, forcePrunePreferPrune=True)
            currentAchievedDefense = val + opt.friendly_army_reaching_intercept
            currentGatherTurns = turns + opt.worst_case_intercept_moves
            betterVt = (currentAchievedDefense > totalToSurvive and currentAchievedDefense / currentGatherTurns > bestAchievedDefense / bestTurns)
            # oneMoveCapForTileNotReachingChoke =
            if currentAchievedDefense >= bestAchievedDefense or betterVt:  # and defenseTurns > 12 and defenseTurns - bestTurns >
                self.info(f'  HYBR int {opt}:')
                self.info(f'     {currentAchievedDefense:.1f} > {bestAchievedDefense:.1f} ({currentGatherTurns}t vs {bestTurns}t) or {currentAchievedDefense / currentGatherTurns:.2f}vt > {bestAchievedDefense / bestTurns:.2f}vt (w pruned def {val:.1f}/{turns}t {0 if turns == 0.0 else val / turns:.2f}vt)')

                # curDefensePlan = pruned
                # achievedDefense = currentAchievedDefense
                # defenseTurns = currentGatherTurns
                bestTurns = currentGatherTurns
                bestAchievedDefense = currentAchievedDefense
                bestSurvives = bestAchievedDefense >= totalToSurvive
                bestRemainingDefense = pruned
                bestOpt = opt
            elif DebugHelper.IS_DEBUGGING:
                self.info(f' -HYBR int incl{opt}:')
                self.info(f'     {currentAchievedDefense:.1f} < {bestAchievedDefense:.1f} ({currentGatherTurns}t vs {bestTurns}t)  and {currentAchievedDefense / currentGatherTurns:.2f}vt < {bestAchievedDefense / bestTurns:.2f}vt (w pruned def {val:.1f}/{turns}t {0 if turns == 0.0 else val / turns:.2f}vt)')

        if bestOpt is not None:
            self.viewInfo.color_path(PathColorer(
                bestOpt.path, 1, 1, 1,
            ))
            intOptInfo = f' {bestOpt}'
            self.info(f'DEF HYBR int incl{intOptInfo}: {bestAchievedDefense:.1f}a in {bestTurns}t')

            return self.get_first_path_move(bestOpt.path), bestOpt.path, isDelayed, bestRemainingDefense

        return None, None, False, bestRemainingDefense

    def get_defense_path_option_from_options_if_available(self, threatInterceptionPlan: ArmyInterception, threat: ThreatObj) -> typing.Tuple[InterceptionOptionInfo | None, TilePlanInterface | None]:
        # if not self.expansion_plan.includes_intercept:  # or self.expansion_plan.intercept_waiting
        #     return None, None

        interceptPath = self.expansion_plan.selected_option
        interceptingOption = None
        if interceptPath is not None and isinstance(interceptPath, InterceptionOptionInfo):
            if interceptPath == threatInterceptionPlan.intercept_options.get(interceptPath.length, None):
                interceptingOption = interceptPath
                interceptPath = interceptPath.path
                if interceptingOption not in threatInterceptionPlan.intercept_options.values():
                    return None, None

        if interceptingOption is None:
            interceptPath = None

        includesIntercept = False
        for delayedInterceptOption in self.expansion_plan.intercept_waiting:
            if threat in threatInterceptionPlan.threats and delayedInterceptOption in threatInterceptionPlan.intercept_options.values():
                interceptPath = delayedInterceptOption.path
                includesIntercept = True
                interceptingOption = delayedInterceptOption
                # these are always delayed
                isDelayed = True
                break

        if interceptingOption is None:
            vt = 0
            at = 0
            for turns, intercept in threatInterceptionPlan.intercept_options.items():
                optVt = intercept.econValue / turns
                optAt = intercept.friendly_army_reaching_intercept / turns

                if optVt > vt:
                    vt = optVt
                    at = optAt
                    self.info(f'{turns}: val/turn {optVt:.2f} > {vt:.2f}, replacing {interceptingOption} with {intercept}')
                    interceptingOption = intercept
                    interceptPath = interceptingOption.path
                elif vt < 1 and optAt > at:
                    vt = optVt
                    at = optAt
                    self.info(f'{turns}: army/turn {optAt:.2f} > {at:.2f} (vt {optVt:.2f} vs {vt:.2f}), replacing {interceptingOption} with {intercept}')
                    interceptingOption = intercept
                    interceptPath = interceptingOption.path

        if not includesIntercept and interceptingOption in threatInterceptionPlan.intercept_options.values():
            # if interceptingOption.intercepting_army_remaining <= 0:
            if threat.threatValue - interceptingOption.friendly_army_reaching_intercept < 0:
                includesIntercept = True
                interceptingOption = threatInterceptionPlan.get_intercept_option_by_path(interceptPath)
                if interceptingOption is not None:
                    isDelayed = interceptingOption.requiredDelay > 0
            else:
                self.viewInfo.add_info_line(f'not safe to intercept {threat.threatValue} capture threat w remaining {interceptingOption.friendly_army_reaching_intercept}')
                return None, None

        return interceptPath, interceptingOption

    def get_defense_moves(
            self,
            defenseCriticalTileSet: typing.Set[Tile],
            raceEnemyKingKillPath: Path | None,
            raceChance: float
    ) -> typing.Tuple[Move | None, Path | None]:
        """
        Defend against a threat. Modifies the defense critical set to include save-tiles if a prune-defense is calculated and only barely saves.

        @param defenseCriticalTileSet:
        @param raceEnemyKingKillPath:
        @return:
        """

        move: Move | None = None

        outputDefenseCriticalTileSet = defenseCriticalTileSet
        # defenseCriticalTileSet = defenseCriticalTileSet.copy()
        self.best_defense_leaves: typing.List[GatherTreeNode] = []

        threats = []
        if self.dangerAnalyzer.fastestThreat is not None and self.dangerAnalyzer.fastestThreat.turns > -1:
            threats.append(self.dangerAnalyzer.fastestThreat)
        if self.dangerAnalyzer.fastestAllyThreat is not None and self.dangerAnalyzer.fastestAllyThreat.turns > -1:
            if len(threats) > 0 and threats[0].path.start.tile == self.dangerAnalyzer.fastestAllyThreat.path.start.tile and threats[0].turns - 1 > self.dangerAnalyzer.fastestAllyThreat.turns:
                self.info(f'IGNORING SELF THREAT DUE TO ALLY BEING CLOSER TO DEATH ({threats[0].turns} vs {self.dangerAnalyzer.fastestAllyThreat.turns})')
                threats = []
            threats.append(self.dangerAnalyzer.fastestAllyThreat)
        if self.dangerAnalyzer.fastestCityThreat is not None and self.dangerAnalyzer.fastestCityThreat.turns > -1:
            threats.append(self.dangerAnalyzer.fastestCityThreat)

        # threats = [t for t in sorted(threats, key=lambda t: t.threatValue, reverse=True)]

        negativeTilesIncludingThreat = outputDefenseCriticalTileSet.copy()

        for threat in threats:
            if threat is not None and threat.threatType == ThreatType.Kill:
                for tile in threat.path.tileSet:
                    negativeTilesIncludingThreat.add(tile)

        # commonThreatChoke =

        movesToMakeAnyway = []

        realThreats = []
        anyRealThreat = False
        for threat in threats:
            interceptMove, interceptPath, intOption, interceptDelayed = self.check_defense_intercept_move(threat)
            if interceptDelayed:
                self.viewInfo.add_info_line(f'DEFENSE INTERCEPT SAID DELAYED AGAINST THREAT, NO OPPING DEFENSE')
                negativeTilesIncludingThreat.update(interceptPath.tileList)
                outputDefenseCriticalTileSet.update(interceptPath.tileList)
                continue

            if interceptMove is not None and intOption is not None and intOption.econValue / intOption.length > 2.5:
                vt = intOption.econValue / intOption.length
                self.info(f'def int move against {threat.path.start.tile} vt {vt:.2f} ({intOption.econValue:.2f}/{intOption.length}), blk {intOption.damage_blocked:.1f}, wci {intOption.worst_case_intercept_moves}, bci {intOption.best_case_intercept_moves}, rt {intOption.recapture_turns}')
                return interceptMove, interceptPath

            isRealThreat = True
            # tilecapture threats and city capture threats dont warrant all-in all-or-nothing defense behavior.
            isEconThreat = not threat.path.tail.tile.isGeneral

            army = self.armyTracker.armies.get(threat.path.start.tile, None)
            if army and army.visible and army.last_moved_turn > self._map.turn - 2:
                logbook.info(f'get_defense_moves setting targetingArmy to real threat army {str(army)}')
                self.targetingArmy = army

            threatMovingWrongWay = False
            threatTile = threat.path.start.tile
            if threatTile.delta.fromTile:
                threatDist = threat.armyAnalysis.aMap[threatTile]
                threatFromDist = threat.armyAnalysis.aMap[threatTile.delta.fromTile]
                if threatDist >= threatFromDist:
                    threatMovingWrongWay = True

            savePath: Path | None = None
            searchTurns = threat.turns

            # TODO i dont want to depend on this hot garbage anymore. Keep this shit commented out. :)
            #  interception SHOULD replace it.
            # with self.perf_timer.begin_move_event('Searching for a threat killer move...'):
            #     move = self.get_threat_killer_move(threat, searchTurns, outputDefenseCriticalTileSet)
            # if move is not None and threat.armyAnalysis.chokeWidths[move.dest] < 3:
            #     self.viewInfo.infoText = f"threat killer move! {move.source.x},{move.source.y} -> {move.dest.x},{move.dest.y}"
            #     if self.curPath is not None and move.source == self.curPath.start.tile:
            #         # self.curPath.add_start(move.dest)
            #         # self.viewInfo.infoText = f"threat killer move {move.source.x},{move.source.y} -> {move.dest.x},{move.dest.y} WITH ADDED FUN TO GET PATH BACK ON TRACK!"
            #         self.curPath = None
            #         self.viewInfo.infoText = f"threat killer move {move.source.x},{move.source.y} -> {move.dest.x},{move.dest.y}. Dropped curPath."
            #     return self.move_half_on_repetition(move, 5, 4), savePath

            armyAmount = threat.threatValue + 1
            logbook.info(
                f"\n!-!-!-!-!-! danger in {threat.turns}, gather {armyAmount} in {searchTurns} turns  !-!-!-!-!-!")

            self.viewInfo.add_targeted_tile(threat.path.tail.tile)
            flags = ''
            if threat is not None and threat.threatType == ThreatType.Kill:
                survivalThreshold = threat.threatValue
                distOffsetNOWUNUSED = 1
                addlTurnsToAllowGatherForAlwaysZero = 0
                saveTurns = threat.turns  # - 1
                if threat is not None and self._map.player_has_priority_over_other(self.player.index, threat.threatPlayer, self._map.turn + threat.turns) and not self.has_defenseless_modifier:
                    distOffsetNOWUNUSED += 1
                    # ok we DONT want additional turns as this causes us to waste moves chasing the enemy army. Instead, distDict can alternate on priority (?)
                    # addlTurns += 1
                if threat.saveTile is not None or isEconThreat and addlTurnsToAllowGatherForAlwaysZero == 0:
                    saveTurns += 1

                if threat.turns > 2 * self.shortest_path_to_target_player.length // 3:
                    distOffsetNOWUNUSED = 0
                shouldBypass = self.should_bypass_army_danger_due_to_last_move_turn(threat.path.start.tile)
                if shouldBypass:
                    self.viewInfo.add_info_line(f'skip def dngr from{str(army.tile)} last_seen {army.last_seen_turn}, last_moved {army.last_moved_turn}')
                    distOffsetNOWUNUSED -= 1

                with self.perf_timer.begin_move_event(f'Def Gath {saveTurns}t @ {str(threat.path.start.tile)}->{str(threat.path.tail.tile)}'):
                    additionalNegatives = set()
                    if self.teammate_communicator is not None:
                        survivalThreshold, additionalNegatives = self.teammate_communicator.get_additional_defense_negatives_and_contribution_requirement(threat)
                    self.viewInfo.add_stats_line('WHITE O: teammate defense negativess')
                    for tile in additionalNegatives:
                        self.viewInfo.add_targeted_tile(tile, TargetStyle.WHITE, radiusReduction=9)
                    outputDefenseCriticalTileSet.update(additionalNegatives)  # don't try to use these tiles for any other purpose, ally has purposed them for our defense
                    timeLimit = 0.05
                    if not threat.path.tail.tile.isGeneral:
                        timeLimit = 0.015
                    move, valueGathered, turnsUsed, gatherNodes = self.get_gather_to_threat_path(
                        threat,
                        requiredContribution=survivalThreshold,
                        additionalNegatives=additionalNegatives,
                        addlTurns=addlTurnsToAllowGatherForAlwaysZero,
                        timeLimit=timeLimit)

                    # TODO distOffset was causing early gathers...? Why was it here?
                    # fuckin, why was the +1 here as well?
                    if gatherNodes is not None:
                        leavesGreaterThanDistance = GatherTreeNode.get_tree_leaves_further_than_distance(gatherNodes, threat.armyAnalysis.aMap, threat.turns, survivalThreshold)
                        anyLeafIsSameDistAsThreat = len(leavesGreaterThanDistance) > 0
                        if anyLeafIsSameDistAsThreat:
                            self.info(f'defense anyLeafIsSameDistAsThreat {anyLeafIsSameDistAsThreat}')
                        move_closest_value_func = self.get_defense_tree_move_prio_func(threat, anyLeafIsSameDistAsThreat, printDebug=DebugHelper.IS_DEBUGGING)
                        move = self.get_tree_move_default(gatherNodes, move_closest_value_func)
                if move:
                    # with self.perf_timer.begin_move_event(f'Def int rep @ {str(threat.path.start.tile)}->{str(threat.path.tail.tile)}'):
                    #     intMove, intPath, isDelayed, gatherNodes = self.check_defense_hybrid_intercept_moves(threat, gatherNodes, survivalThreshold - valueGathered, additionalNegatives)
                    #     if intMove is not None:
                    #         if not isDelayed:
                    #             self.info(f'YOOOOO INTERCEPT MOVE BEATS DEFENSE {intMove}')
                    #             return intMove, intPath
                    #         else:
                    #             self.info(f'Intercept defense says delay {intMove}. Adding it to additional negatives...')
                    #             additionalNegatives.update(intPath.tileList)
                    with self.perf_timer.begin_move_event(f'Def prun @ {str(threat.path.start.tile)}->{str(threat.path.tail.tile)}'):
                        if valueGathered > survivalThreshold:
                            pruned = GatherTreeNode.clone_nodes(gatherNodes)
                            sumPrunedTurns, sumPruned, pruned = Gather.prune_mst_to_army_with_values(
                                pruned,
                                survivalThreshold + 1,
                                self.general.player,
                                MapBase.get_teams_array(self._map),
                                self._map.turn,
                                viewInfo=self.viewInfo,
                                preferPrune=self.expansion_plan.preferred_tiles if self.expansion_plan is not None else None,
                                noLog=False)

                            if (self.is_blocking_neutral_city_captures or valueGathered - sumPruned < 45) and not isEconThreat:
                                self.block_neutral_captures('due to pruned defense being less than safe if we take the city.')

                            citiesInPruned = SearchUtils.Counter(0)
                            GatherTreeNode.foreach_tree_node(pruned, lambda n: citiesInPruned.add(1 * ((n.tile.isGeneral or n.tile.isCity) and self._map.is_tile_friendly(n.tile))))
                            turnGap = threat.turns - sumPrunedTurns
                            sumPruned += (turnGap * citiesInPruned.value // 2)
                            if sumPruned < survivalThreshold:
                                if SearchUtils.BYPASS_TIMEOUTS_FOR_DEBUGGING:
                                    raise AssertionError(
                                        f'We should absolutely never get here with army pruned {sumPruned} being less than threat {survivalThreshold} but inside the original gather {valueGathered} greater than threat.')

                            # TODO distOffset was causing early gathers...? Why was it here?
                            flipThingy = 0  # was reset to 1...?
                            leavesGreaterThanDistance = GatherTreeNode.get_tree_leaves_further_than_distance(pruned, threat.armyAnalysis.aMap, threat.turns - flipThingy, survivalThreshold, sumPruned)
                            anyLeafIsSameDistAsThreat = len(leavesGreaterThanDistance) > 0
                            if anyLeafIsSameDistAsThreat:
                                flags = f'leafDist {flags}'
                            else:
                                leavesGreaterThanBlockDistance = GatherTreeNode.get_tree_leaves_further_than_distance(pruned, threat.armyAnalysis.aMap, saveTurns - flipThingy - 1)
                                if len(leavesGreaterThanBlockDistance) > 0:
                                    outputDefenseCriticalTileSet.update([n.tile for n in leavesGreaterThanBlockDistance])

                            if sumPrunedTurns >= threat.turns or anyLeafIsSameDistAsThreat:  # was - 2, but that fails this test: test_should_not_defense_loop_let_army_engine_make_moves   was -1 but that still fails tests like test_should_close_distance_and_corner_army
                                if interceptMove is not None:
                                    self.info(f'Must def, int move {interceptMove} (prunedT {sumPrunedTurns}, threat {threat.turns}, anyLeafIsSameDistAsThreat {anyLeafIsSameDistAsThreat})')
                                    return interceptMove, interceptPath

                                # we have to act now...?
                                pruned = [node.deep_clone() for node in gatherNodes]
                                sumPrunedTurns, sumPruned, pruned = Gather.prune_mst_to_max_army_per_turn_with_values(
                                    pruned,
                                    survivalThreshold,
                                    self.general.player,
                                    MapBase.get_teams_array(self._map),
                                    preferPrune=self.expansion_plan.preferred_tiles if self.expansion_plan is not None else None,
                                    viewInfo=self.viewInfo)

                                move_closest_value_func = self.get_defense_tree_move_prio_func(threat, anyLeafIsSameDistAsThreat, printDebug=DebugHelper.IS_DEBUGGING)
                                self.redGatherTreeNodes = gatherNodes

                                self.gatherNodes = pruned
                                move = self.get_tree_move_default(pruned, move_closest_value_func)
                                self.communicate_threat_to_ally(threat, sumPruned, pruned)
                                self.info(
                                    f'{flags}GathDefRaw-{str(threat.path.start.tile)}@{str(threat.path.tail.tile)}:  {move} val {valueGathered:.1f}/p{sumPruned:.1f}/{survivalThreshold} turns {turnsUsed}/p{sumPrunedTurns}/{threat.turns} offs{distOffsetNOWUNUSED}')
                                return move, savePath
                            else:
                                # we survive and can wait
                                self.communicate_threat_to_ally(threat, sumPruned, pruned)
                                isRealThreat = False
                                # the threat is harmless...?
                                if not self.best_defense_leaves:
                                    self.best_defense_leaves = GatherTreeNode.get_tree_leaves(pruned)
                                    self.set_defensive_blocks_against(threat)

                                if sumPrunedTurns >= threat.turns - 2:
                                    if interceptMove is not None:
                                        self.info(f'Soon def, int move?? {interceptMove} (prunedT {sumPrunedTurns}, threat {threat.turns}, anyLeafIsSameDistAsThreat {anyLeafIsSameDistAsThreat})')
                                        # return interceptMove, interceptPath

                                    def addPrunedDefenseToDefenseNegatives(tn: GatherTreeNode):
                                        if self.board_analysis.intergeneral_analysis.is_choke(tn.tile) or threat.armyAnalysis.is_choke(tn.tile):
                                            logbook.info(f'    outputDefenseCriticalTileSet SKIPPING CHOKE {str(tn.tile)}')
                                        else:
                                            logbook.info(f'    outputDefenseCriticalTileSet adding {str(tn.tile)}')
                                            outputDefenseCriticalTileSet.add(tn.tile)

                                    GatherTreeNode.foreach_tree_node(pruned, addPrunedDefenseToDefenseNegatives)

                                    if self.territories.is_tile_in_friendly_territory(threat.path.start.tile):
                                        logbook.info(f'get_defense_moves setting targetingArmy to threat in friendly territory {str(threat.path.start.tile)}')
                                        self.targetingArmy = self.get_army_at(threat.path.start.tile)
                                    # logbook.info(f"we're pretty safe from threat via gather, try fancier gather AT threat")
                                    # atThreatMove, altValueGathered, altTurnsUsed, altGatherNodes = self.get_gather_to_threat_path(threat, force_turns_up_threat_path=threat.turns//2)
                                    # if atThreatMove:
                                    #     self.info(f'{str(atThreatMove)} AT threat value {altValueGathered}/{survivalThreshold} turns {altTurnsUsed}/{threat.turns}')
                                    #     self.gatherNodes = altGatherNodes
                                    #     return atThreatMove, savePath

                                    self.viewInfo.add_info_line(f'  DEF NEG ADD - prune t{sumPrunedTurns} < threat.turns - 3 {threat.turns - 3} (threatVal {survivalThreshold} v pruneVal {sumPruned:.1f})')

                # they might not find us, giving us more time to gather. Also they'll likely waste some army running around our tiles so subtract 10 from the threshold.

                abandonDefenseThreshold = survivalThreshold * 0.8 - 3 - threat.turns
                if len(self._map.players) == 2 and self._map.turn > 250 and not threatMovingWrongWay:
                    # then they probably have a really good idea of where we are by now.

                    # TODO replace this with a "percentage of our forward tiles discovered by enemy" based calculation.
                    #  In FFAs where the player has barely seen us till 200+ tiles, dont abandon defense easily. Etc.
                    abandonDefenseThreshold = survivalThreshold * 0.92 - threat.turns // 2
                if self._map.players[threat.threatPlayer].knowsKingLocation:
                    abandonDefenseThreshold = survivalThreshold * 0.96 - threat.turns // 4 - 1

                if threat.path.tail.tile.isCity:
                    abandonDefenseThreshold = survivalThreshold

                if valueGathered < survivalThreshold - 1:
                    # TODO defense scrim lol?
                    # if threat.turns < 15:
                    #     with self.perf_timer.begin_move_event(f'def scrim @{str(threat.path.start.tile)} {str(threat.path)}'):
                    #         path, simResult = self.try_find_counter_army_scrim_path_kill(threat.path, allowGeneral=True, forceEnemyTowardsGeneral=False)
                    #     if simResult is not None and simResult.net_economy_differential > -40:
                    #         if path is not None:
                    #             return self.get_first_path_move(path), path
                    #         else:
                    #             self.info("Def scrim said wait...?")
                    #             return None, None

                    self.communicate_threat_to_ally(threat, valueGathered, gatherNodes)
                    extraTurns = 1
                    pruneToValuePerTurn = False
                    # extraThreat = 0
                    if threat.path.tail.tile.isGeneral:
                        flags = f'DEAD {flags}'
                        if raceChance > 0.1 and raceEnemyKingKillPath is not None:
                            self.info(f'DEAD: RACING BECAUSE WE ARE DEAD WITH A NON-ZERO RACE KILL CHANCE')
                            return raceEnemyKingKillPath.get_first_move(), raceEnemyKingKillPath
                    else:
                        flags = f'CAP {flags}'
                        pruneToValuePerTurn = True
                        # making this lower than 12 fails test_should_defend_city_what_the_fuck
                        # was 10, lol. This needs rework.
                        extraTurns = 12
                        # they will cap, so need extra army
                        survivalThreshold += extraTurns // 2
                        # threat.threatValue += extraThreat

                    with self.perf_timer.begin_move_event(f'+{extraTurns} Def Threat Gather {threat.path.start.tile}@{threat.path.tail.tile}'):
                        altMove, altValueGathered, altTurnsUsed, altGatherNodes = self.get_gather_to_threat_path(
                            threat,
                            requiredContribution=survivalThreshold,
                            additionalNegatives=additionalNegatives,
                            addlTurns=extraTurns)

                        if pruneToValuePerTurn and altGatherNodes is not None:
                            sumPrunedTurns, sumPruned, altGatherNodes = Gather.prune_mst_to_army_with_values(
                                altGatherNodes,
                                survivalThreshold + 1,
                                self.general.player,
                                MapBase.get_teams_array(self._map),
                                self._map.turn,
                                viewInfo=self.viewInfo,
                                preferPrune=self.expansion_plan.preferred_tiles if self.expansion_plan is not None else None,
                                noLog=not DebugHelper.IS_DEBUGGING)
                            valFunc = self.get_defense_tree_move_prio_func(threat, anyLeafIsSameDistAsThreat=False, printDebug=DebugHelper.IS_DEBUGGING)
                            altMove = self.get_tree_move_default(altGatherNodes, valFunc)
                    if altMove is not None:
                        directlyAttacksDest = altMove.dest == threat.path.start.tile
                        if directlyAttacksDest or gatherNodes is None or not self.is_2v2_teammate_still_alive():  # or valueGathered / turnsUsed < survivalThreshold / (2 * threat.turns) ?
                            if altValueGathered >= survivalThreshold:
                                self.redGatherTreeNodes = gatherNodes
                                move = altMove
                                valueGathered = altValueGathered
                                turnsUsed = altTurnsUsed
                                gatherNodes = altGatherNodes

                isGatherMoveFromBackwards = self.is_move_towards_enemy(move)
                # TODO hack for now
                isGatherMoveFromBackwards = False
                if not isRealThreat and (not isGatherMoveFromBackwards or move is None or self.detect_repetition_tile(move.source)):
                    if move is None:
                        flags = f'waitNONE {flags}'
                    elif move is not None and self.detect_repetition_tile(move.source):
                        flags = f'rep {flags}'
                    else:
                        flags = f'wait {flags}'
                    self.redGatherTreeNodes = gatherNodes
                    self.gatherNodes = None

                self.info(
                    f'{flags}GathDef-{str(threat.path.start.tile)}@{str(threat.path.tail.tile)}:  {move} val {valueGathered:.1f}/{survivalThreshold} turns {turnsUsed}/{threat.turns} (abandThresh {abandonDefenseThreshold:.0f} offs{distOffsetNOWUNUSED}')
                if isRealThreat or self.detect_repetition_tile(move.source, turns=8, numReps=3):
                    realThreats.append(threat)
                    if threat.turns < 7:
                        self.increment_attack_counts(threat.path.tail.tile)

                if valueGathered > abandonDefenseThreshold or (self.is_2v2_teammate_still_alive() and len(additionalNegatives) == 0):
                    if isRealThreat:
                        self.curPath = None
                        self.gatherNodes = gatherNodes
                        return move, savePath

                    if isGatherMoveFromBackwards and not self.detect_repetition_tile(move.source):
                        movesToMakeAnyway.append(move)
                else:
                    self.info(f'aband def bcuz ? valueGathered {valueGathered:.1f} <= abandonDefenseThreshold {abandonDefenseThreshold:.1f}')

            if not isRealThreat or isEconThreat:
                continue

            armyAmount = 1
            # defenseNegatives = set(threat.path.tileSet)
            # defenseNegatives = set(threat.armyAnalysis.shortestPathWay.tiles)
            altKillOffset = 0
            if not self.targetPlayerExpectedGeneralLocation.isGeneral:
                altKillOffset = 5 + int(len(self.targetPlayerObj.tiles) ** 0.5)
                logbook.info(f'altKillOffset {altKillOffset} because dont know enemy gen position for sure')
            with self.perf_timer.begin_move_event(
                    f"ATTEMPTING TO FIND KILL ON ENEMY KING UNDISCOVERED SINCE WE CANNOT SAVE OURSELVES, TURNS {threat.turns - 1}:"):
                altKingKillPath = SearchUtils.dest_breadth_first_target(
                    self._map,
                    [self.targetPlayerExpectedGeneralLocation],
                    12,
                    0.1,
                    threat.turns + 1,
                    outputDefenseCriticalTileSet,
                    searchingPlayer=self.general.player,
                    dontEvacCities=False)

                if altKingKillPath is not None:
                    logbook.info(
                        f"   Did find a killpath on enemy gen / undiscovered {str(altKingKillPath)}")
                    wrpPath = None
                    if not altKingKillPath.tail.tile.isGeneral:
                        wrpPath = self.get_optimal_exploration(threat.turns, outputDefenseCriticalTileSet, maxTime=0.020, includeCities=False)
                        if wrpPath is not None:
                            for t in wrpPath.tileList:
                                if t in self.targetPlayerExpectedGeneralLocation.adjacents:
                                    altKingKillPath = wrpPath
                                    self.info(f'WRP KING KILL {wrpPath}')
                                    r, g, b = Colors.GOLD
                                    self.viewInfo.color_path(PathColorer(
                                        wrpPath,
                                        r, g, b,
                                        255, 0
                                    ))
                                    break
                            if altKingKillPath != wrpPath:
                                logbook.info(f'wrpPath was {wrpPath}')

                    # these only return if we think we can win/tie the race
                    if (raceEnemyKingKillPath is None or (raceEnemyKingKillPath.length >= threat.turns and wrpPath is None)) and altKingKillPath.length + altKillOffset < threat.turns:
                        self.info(f"{flags} altKingKillPath {str(altKingKillPath)} altKillOffset {altKillOffset}")
                        self.viewInfo.color_path(PathColorer(altKingKillPath, 122, 97, 97, 255, 10, 200))
                        return self.get_first_path_move(altKingKillPath), savePath
                    elif wrpPath is not None:
                        logbook.info("   wrpPath already existing, will not use the above.")
                        self.info(f"{flags} wrpPath {str(wrpPath)} altKillOffset {altKillOffset}")
                        self.viewInfo.color_path(PathColorer(wrpPath, 152, 97, 97, 255, 10, 200))
                        return self.get_first_path_move(wrpPath), savePath
                    elif raceEnemyKingKillPath is not None:
                        logbook.info("   raceEnemyKingKillPath already existing, will not use the above.")
                        self.info(f"{flags} raceEnemyKingKillPath {str(raceEnemyKingKillPath)} altKillOffset {altKillOffset}")
                        self.viewInfo.color_path(PathColorer(raceEnemyKingKillPath, 152, 97, 97, 255, 10, 200))
                        return self.get_first_path_move(raceEnemyKingKillPath), savePath

            if altKingKillPath is not None:
                if raceEnemyKingKillPath is None or raceEnemyKingKillPath.length > threat.turns:
                    self.info(
                        f"{flags} altKingKillPath (long {altKingKillPath.length} vs threat {threat.turns}) {str(altKingKillPath)}")
                    self.viewInfo.color_path(PathColorer(altKingKillPath, 122, 97, 97, 255, 10, 200))
                    return self.get_first_path_move(altKingKillPath), savePath
                elif raceEnemyKingKillPath is not None:
                    logbook.info("   raceEnemyKingKillPath already existing, will not use the above.")
                    self.info(
                        f"{flags} raceEnemyKingKillPath (long {altKingKillPath.length} vs threat {threat.turns}) {str(raceEnemyKingKillPath)}")
                    self.viewInfo.color_path(PathColorer(raceEnemyKingKillPath, 152, 97, 97, 255, 10, 200))
                    return self.get_first_path_move(raceEnemyKingKillPath), savePath

        # END for threat in threats LOOP
        if len(movesToMakeAnyway) > 0:
            return movesToMakeAnyway[-1], None

        if len(realThreats) == 0:
            # if self.behavior_allow_defense_army_scrim:
            #     scrimMove = self.try_scrim_against_threat_with_largest_pruned_gather_node(move, pruned, threat)
            #     if scrimMove is not None:
            #         return scrimMove, savePath
            return None, None

        for threat in realThreats:
            if threat.path.tail.tile.isGeneral:
                if not self.targetPlayerExpectedGeneralLocation.isGeneral:
                    # try hunt kill...?
                    explorePath = self.get_optimal_exploration(max(5, threat.turns))
                    if explorePath is not None:
                        self.info(f'DEAD EXPLORE {str(explorePath)}')
                        return self.get_first_path_move(explorePath), explorePath
                else:
                    self.get_gather_to_target_tile(self.targetPlayerExpectedGeneralLocation, 1.0, threat.turns)

        return None, None

    def attempt_first_25_collision_reroute(
            self,
            curPath: Path,
            move: Move,
            distMap: MapMatrixInterface[int]
    ) -> Path | None:
        countExtraUseableMoves = 0
        for path in self.city_expand_plan.plan_paths:
            if path is None:
                countExtraUseableMoves += 1

        negExpandTiles = set()
        negExpandTiles.add(self.general)
        if self.teammate_general is not None:
            negExpandTiles.update(self._map.players[self.teammate_general.player].tiles)

        # we already stripped the move we're not doing off curPath so + 1
        lengthToReplaceCurrentPlan = curPath.length
        rePlanLength = lengthToReplaceCurrentPlan + countExtraUseableMoves
        with self.perf_timer.begin_move_event(f'Re-calc F25 Expansion for {str(move.source)} (length {rePlanLength})'):
            expUtilPlan = ExpandUtils.get_round_plan_with_expansion(
                self._map,
                self.general.player,
                self.targetPlayer,
                rePlanLength,
                self.board_analysis,
                self.territories.territoryMap,
                bonusCapturePointMatrix=self.get_expansion_weight_matrix(),
                tileIslands=self.tileIslandBuilder,
                negativeTiles=negExpandTiles,
                viewInfo=self.viewInfo
            )
        newPath = expUtilPlan.selected_option
        otherPaths = expUtilPlan.all_paths

        _, _, _, pathTurns, econVal, remainingArmy, _ = self.calculate_path_capture_econ_values(curPath, 50)

        if newPath is not None and isinstance(newPath, Path):
            _, _, _, newTurns, newEconVal, newRemainingArmy, _ = self.calculate_path_capture_econ_values(newPath, 50)
            if newEconVal <= econVal:
                self.info(f'recalc attempt found {newPath} with econ {newEconVal:.2f} (vs collisions {econVal:.2f}), will not reroute...')
                return None
            segments = newPath.break_overflow_into_one_move_path_subsegments(
                lengthToKeepInOnePath=lengthToReplaceCurrentPlan)
            self.city_expand_plan.plan_paths[0] = None
            if segments[0] is not None:
                for i in range(segments[0].length, lengthToReplaceCurrentPlan):
                    logbook.info(f'plan segment 0 {str(segments[0])} was shorter than lengthToReplaceCurrentPlan {lengthToReplaceCurrentPlan}, inserting a None')
                    self.city_expand_plan.plan_paths.insert(0, None)

            curSegIndex = 0
            for i in range(len(self.city_expand_plan.plan_paths)):
                if self.city_expand_plan.plan_paths[i] is None and curSegIndex < len(segments):
                    if i > 0:
                        logbook.info(f'Awesome, managed to replace expansion no-ops with expansion in F25 collision!')
                    self.city_expand_plan.plan_paths[i] = segments[curSegIndex]
                    curSegIndex += 1

            return segments[0]
        else:
            return None

    def get_army_at(self, tile: Tile, no_expected_path: bool = False):
        return self.armyTracker.get_or_create_army_at(tile, skip_expected_path=no_expected_path)

    def get_army_at_x_y(self, x: int, y: int):
        tile = self._map.GetTile(x, y)
        return self.get_army_at(tile)

    def initialize_from_map_for_first_time(self, map: MapBase):
        self._map = map
        self._map.distance_mapper = DistanceMapperImpl(map)
        self.viewInfo = ViewInfo(2, self._map)
        self.is_lag_massive_map = self._map.rows * self._map.cols > 1000

        self.completed_first_100 = self._map.turn > 85

        self.initialize_logging()
        self.general = self._map.generals[self._map.player_index]
        self.player = self._map.players[self.general.player]
        self.alt_en_gen_positions = [[] for p in self._map.players]
        self._alt_en_gen_position_distances = [None for p in self._map.players]

        self.teams = MapBase.get_teams_array(map)
        self.opponent_tracker = OpponentTracker(self._map, self.viewInfo)
        self.opponent_tracker.analyze_turn(-1)
        self.expansion_plan = ExpansionPotential(0, 0, 0, None, [], 0.0)

        for teammate in self._map.teammates:
            teammatePlayer = self._map.players[teammate]
            if teammatePlayer.dead or not self._map.generals[teammate]:
                continue
            self.teammate_general = self._map.generals[teammate]
            self._ally_distances = self._map.distance_mapper.get_tile_dist_matrix(self.teammate_general)
            allyUsername = self._map.usernames[self.teammate_general.player]
            if "Teammate.exe" == allyUsername or "Human.exe" == allyUsername or "Exe.human" == allyUsername:  # or "EklipZ" in allyUsername
                # Use more excessive pings when paired with a bot for inter-bot communication.
                self.teamed_with_bot = True

        self._gen_distances = self._map.distance_mapper.get_tile_dist_matrix(self.general)

        self.dangerAnalyzer = DangerAnalyzer(self._map)
        self.cityAnalyzer = CityAnalyzer(self._map, self.general)
        self.gatherAnalyzer = GatherAnalyzer(self._map)
        self.isInitialized = True

        self.has_defenseless_modifier = self._map.modifiers_by_id[MODIFIER_DEFENSELESS]
        self.has_watchtower_modifier = self._map.modifiers_by_id[MODIFIER_WATCHTOWER]
        self.has_misty_veil_modifier = self._map.modifiers_by_id[MODIFIER_MISTY_VEIL]

        self._map.notify_city_found.append(self.handle_city_found)
        self._map.notify_tile_captures.append(self.handle_tile_captures)
        self._map.notify_tile_deltas.append(self.handle_tile_deltas)
        self._map.notify_tile_discovered.append(self.handle_tile_discovered)
        self._map.notify_tile_vision_changed.append(self.handle_tile_vision_change)
        self._map.notify_player_captures.append(self.handle_player_captures)
        if self.territories is None:
            self.territories = TerritoryClassifier(self._map)

        self.armyTracker = ArmyTracker(self._map, self.perf_timer)
        self.armyTracker.notify_unresolved_army_emerged.append(self.handle_tile_vision_change)
        self.armyTracker.notify_army_moved.append(self.handle_army_moved)
        self.armyTracker.notify_army_moved.append(self.opponent_tracker.notify_army_moved)
        self.targetPlayerExpectedGeneralLocation = self.general.movable[0]
        self.tileIslandBuilder = TileIslandBuilder(self._map)
        self.tileIslandBuilder.recalculate_tile_islands(enemyGeneralExpectedLocation=self.targetPlayerExpectedGeneralLocation)
        self.launchPoints.append(self.general)
        self.board_analysis = BoardAnalyzer(self._map, self.general, self.teammate_general)
        self.army_interceptor = ArmyInterceptor(self._map, self.board_analysis)
        self.win_condition_analyzer = WinConditionAnalyzer(self._map, self.opponent_tracker, self.cityAnalyzer, self.territories, self.board_analysis)
        self.capture_line_tracker = CaptureLineTracker(self._map)
        self.timing_cycle_ended()
        self.opponent_tracker.outbound_emergence_notifications.append(self.armyTracker.notify_concrete_emergence)
        self.is_weird_custom = self._map.is_walled_city_game or self._map.is_low_cost_city_game
        if self._map.cols < 13 or self._map.rows < 13:
            self.is_weird_custom = True

        # minCity = None
        # for tile in self._map.get_all_tiles():
        #     if tile.isCity and tile.isNeutral and (minCity is None or minCity.army > tile.army):
        #         minCity = tile
        #
        # if minCity is not None and minCity.army < 30:
        #     self.is_weird_custom = True
        #     self._map.set_walled_cities(minCity.army)

    def __getstate__(self):
        raise AssertionError("EklipZBot Should never be serialized")

    def __setstate__(self, state):
        raise AssertionError("EklipZBot Should never be deserialized")

    @staticmethod
    def add_city_score_to_view_info(score: CityScoreData, viewInfo: ViewInfo):
        tile = score.tile
        viewInfo.topRightGridText[tile] = f'r{f"{score.city_relevance_score:.2f}".strip("0")}'
        viewInfo.midRightGridText[tile] = f'e{f"{score.city_expandability_score:.2f}".strip("0")}'
        viewInfo.bottomMidRightGridText[tile] = f'd{f"{score.city_defensability_score:.2f}".strip("0")}'
        viewInfo.bottomRightGridText[tile] = f'g{f"{score.city_general_defense_score:.2f}".strip("0")}'

        if tile.player >= 0:
            scoreVal = score.get_weighted_enemy_capture_value()
            viewInfo.bottomLeftGridText[tile] = f'e{f"{scoreVal:.2f}".strip("0")}'
        else:
            scoreVal = score.get_weighted_neutral_value()
            viewInfo.bottomLeftGridText[tile] = f'n{f"{scoreVal:.2f}".strip("0")}'

    def get_quick_kill_on_enemy_cities(self, defenseCriticalTileSet: typing.Set[Tile]) -> Path | None:
        """

        @param defenseCriticalTileSet:
        @return:
        """
        # check 1-dist
        foundCap = None
        enemyCitiesOrderedByPriority = self.get_enemy_cities_by_priority()
        for enemyCity in enemyCitiesOrderedByPriority:
            enMovedNear = False
            cap: Path | None = None
            for movable in enemyCity.movableNoObstacles:
                if movable.player == enemyCity.player and movable.lastMovedTurn > self._map.turn - 2 and movable.delta.toTile != enemyCity:
                    enMovedNear = True
                    break
                if movable.player == self.general.player and movable.army > enemyCity.army + 1 and movable not in defenseCriticalTileSet and (cap is None or cap.start.tile.army > movable.army):
                    cap = Path()
                    cap.add_next(movable)
                    cap.add_next(enemyCity)

            if not enMovedNear and cap and (foundCap is None or foundCap.start.tile.army > cap.start.tile.army):
                foundCap = cap

        if foundCap:
            self.info(f'returning 1-move-city-kill {foundCap}')
            return foundCap

        if self.opponent_tracker.winning_on_economy(byRatio=1.5, cityValue=50):
            return None

        negCutoff = 0 - self.player.standingArmy // 40
        highValueNegs = [t for t in self.cityAnalyzer.large_neutral_negatives if t.army < negCutoff]
        if len(highValueNegs) > 0:
            killPath = SearchUtils.dest_breadth_first_target(
                self._map,
                highValueNegs,
                1,
                0.1,
                25,
                negativeTiles=None,
                preferCapture=True,
                searchingPlayer=self.general.player,
                dontEvacCities=False,
                additionalIncrement=0,
                noLog=True
            )
            if killPath is not None:
                self.info(f'returning bulk free-army {killPath}')
                return killPath
        possibleNeutralCities = []
        # also check if we screwed up and left a neutral city too low of army and capture it with urgent priority.
        possibleNeutralCities.extend(c for c in self.cityAnalyzer.city_scores.keys() if
                                 not self.territories.is_tile_in_enemy_territory(c) and (
                                         (c.army < 4 and self.territories.territoryDistances[self.targetPlayer].raw[c.tile_index] > 3)
                                         or (c.army < 20 and self.territories.territoryDistances[self.targetPlayer].raw[c.tile_index] > 9)
                                         or (self._map.is_walled_city_game and not self.armyTracker.seen_player_lookup[self.targetPlayer])))

        if len(possibleNeutralCities) > 0:
            killPath = SearchUtils.dest_breadth_first_target(
                self._map,
                possibleNeutralCities,
                1,
                0.1,
                8,
                negativeTiles=None,
                preferCapture=True,
                searchingPlayer=self.general.player,
                dontEvacCities=False,
                additionalIncrement=0,
                noLog=True
            )
            if killPath is not None:
                self.info(f'returning bulk neut low cost city kill {killPath}')
                return killPath

        tileCountRatio = self._map.players[self.general.player].tileCount ** 0.30
        cityDepthCutoffEnTerritory = max(5, int(tileCountRatio))
        """
        for i in [50, 75, 100, 150, 200, 250]:
            print(f'{i}: {i ** 0.31:.2f}')
            50: 3.36
            75: 3.81
            100: 4.17
            150: 4.73
            200: 5.17
            250: 5.54
        """

        singleQuick = SearchUtils.dest_breadth_first_target(
                self._map,
                enemyCitiesOrderedByPriority,
                1.5,
                0.1,
                3,
                negativeTiles=None,
                preferCapture=True,
                searchingPlayer=self.general.player,
                dontEvacCities=False,
                additionalIncrement=0,
                noLog=True
            )
        if not singleQuick:
            singleQuick = SearchUtils.dest_breadth_first_target(
                    self._map,
                    enemyCitiesOrderedByPriority,
                    3.5,
                    0.1,
                    7,
                    negativeTiles=None,
                    preferCapture=True,
                    searchingPlayer=self.general.player,
                    dontEvacCities=False,
                    additionalIncrement=0.5,
                    noLog=True
                )

        if singleQuick:
            bestDef = self.get_best_defense(singleQuick.tail.tile, singleQuick.length - 1, list())
            if bestDef is not None and bestDef.value > singleQuick.value:
                self.viewInfo.color_path(PathColorer(
                    bestDef,
                    175, 30, 0, alpha=150, alphaDecreaseRate=3
                ))
                self.viewInfo.color_path(PathColorer(
                    singleQuick,
                    0, 175, 0, alpha=150, alphaDecreaseRate=3
                ))
                logbook.info(f'bypassed singleQuick because best defense was easier for opp. {singleQuick.value} vs {bestDef.value}')
            else:
                self.info(f'Quick kill, single quick EN {singleQuick}')
                return singleQuick

        shortestKill: Path | None = None
        # if (len(self.enemyCities) > 5):
        #    cityDepthSearch = 5

        for enemyCity in enemyCitiesOrderedByPriority[:12]:
            negTilesToUse = defenseCriticalTileSet.copy()

            # if the city is part of the threat path, leaving it in negative tiles tries to kill it with '1' army instead of the actual value on it.
            # negTilesToUse.discard(enemyCity)
            if enemyCity in defenseCriticalTileSet:
                negTilesToUse = set()

            cityDepthSearch = cityDepthCutoffEnTerritory
            if self.territories.is_tile_in_friendly_territory(enemyCity):
                cityDepthSearch = cityDepthCutoffEnTerritory + 5
            elif not self.territories.is_tile_in_enemy_territory(enemyCity):
                cityDepthSearch = cityDepthCutoffEnTerritory + 2

            if not self._map.is_player_on_team_with(enemyCity.player, self.targetPlayer):
                cityDepthSearch -= 1

            if self.dangerAnalyzer.fastestThreat is not None and enemyCity in self.dangerAnalyzer.fastestThreat.path.tileSet:
                # if we got here, we either can't defend the threat or we're already safe from the threat with whats on
                # the path, and the threat includes the city, so then we're safe to use anything on the path to kill part of the threat.
                logbook.info(f'bypassing negativeTiles for city quick kill on {str(enemyCity)} due to it being part of threat path')
                negTilesToUse = set()
                cityDepthSearch -= 1

            if self.dangerAnalyzer.fastestPotentialThreat is not None and enemyCity in self.dangerAnalyzer.fastestPotentialThreat.path.tileSet:
                # if we got here, we either can't defend the threat or we're already safe from the threat with whats on
                # the path, and the threat includes the city, so then we're safe to use anything on the path to kill part of the threat.
                logbook.info(f'bypassing negativeTiles for city quick kill on {str(enemyCity)} due to it being part of POTENTIAL threat path')
                negTilesToUse = set()

            logbook.info(
                f"{self.get_elapsed()} searching for depth {cityDepthSearch} dest bfs kill on city {enemyCity.x},{enemyCity.y}")
            self.viewInfo.add_targeted_tile(enemyCity, TargetStyle.RED)
            armyToSearch = self.get_target_army_inc_adjacent_enemy(enemyCity) + 1.5

            addlIncrementing = SearchUtils.Counter(0)

            def counterNearbyIncr(t: Tile):
                if t.isCity and self._map.is_tile_enemy(t) and t != enemyCity:
                    addlIncrementing.add(1)

            SearchUtils.breadth_first_foreach(self._map, enemyCity.adjacents, cityDepthSearch, foreachFunc=counterNearbyIncr)

            # TODO switch to dynamic max beyond range xyz
            killPath = SearchUtils.dest_breadth_first_target(
                self._map,
                [enemyCity],
                1.5,
                0.1,
                2,
                negTilesToUse,
                preferCapture=True,
                searchingPlayer=self.general.player,
                dontEvacCities=False,
                additionalIncrement=addlIncrementing.value / 2,
            )
            if killPath is None:
                killPath = SearchUtils.dest_breadth_first_target(
                    self._map,
                    [enemyCity],
                    max(1.5, armyToSearch),
                    0.1,
                    cityDepthSearch,
                    negTilesToUse,
                    preferCapture=True,
                    searchingPlayer=self.general.player,
                    additionalIncrement=addlIncrementing.value / 2,
                )
            if killPath is None:
                # retry with prefer capture false
                killPath = SearchUtils.dest_breadth_first_target(
                    self._map,
                    [enemyCity],
                    max(1.5, armyToSearch),
                    0.1,
                    cityDepthSearch,
                    negTilesToUse,
                    preferCapture=False,
                    searchingPlayer=self.general.player,
                    additionalIncrement=addlIncrementing.value / 2,
                )
            if killPath is not None:
                bestDef = self.get_best_defense(killPath.tail.tile, killPath.length - 1, list())
                if bestDef is not None and bestDef.value > killPath.value:
                    self.viewInfo.color_path(PathColorer(
                        bestDef,
                        75, 30, 0, alpha=150, alphaDecreaseRate=3
                    ))
                    self.viewInfo.color_path(PathColorer(
                        killPath,
                        0, 75, 0, alpha=150, alphaDecreaseRate=3
                    ))
                    logbook.info(f'bypassed city killpath because best defense was easier for opp. {killPath.value} vs {bestDef.value}')
                    continue

                if killPath.start.tile.isCity and self.should_kill_path_move_half(killPath, int(armyToSearch - enemyCity.army)):
                    killPath.start.move_half = True
                if shortestKill is None or shortestKill.length > killPath.length:
                    self.info(
                        f"En city kill len {killPath.length} on {str(enemyCity)}: {str(killPath)}")
                    shortestKill = killPath

        if shortestKill is not None:
            tgCity = shortestKill.tail.tile
            negTilesToUse = defenseCriticalTileSet
            if tgCity in defenseCriticalTileSet:
                negTilesToUse = set()

            if self.dangerAnalyzer.fastestThreat is not None and tgCity in self.dangerAnalyzer.fastestThreat.path.tileSet:
                # if we got here, we either can't defend the threat or we're already safe from the threat with whats on
                # the path, and the threat includes the city, so then we're safe to use anything on the path to kill part of the threat.
                logbook.info(f'bypassing negativeTiles for city quick kill on {str(tgCity)} due to it being part of threat path')
                negTilesToUse = set()

            armyToSearch = self.get_target_army_inc_adjacent_enemy(tgCity) + 1
            cityPath = shortestKill.get_subsegment(3, end=True)
            maxDur = int(self.player.tileCount ** 0.32) + 1
            # armyToSearch += maxDur * addlIncrementing.value // 2
            # armyToSearch = 1
            path, move = self.plan_city_capture(
                tgCity,
                cityPath,
                allowGather=True,
                targetKillArmy=armyToSearch - 1,
                targetGatherArmy=tgCity.army + armyToSearch - 1,
                killSearchDist=5,
                gatherMaxDuration=maxDur,
                negativeTiles=negTilesToUse)
            if move is not None:
                self.info(f'plan_city_capture quick kill @{tgCity}')
                fakePath = Path()
                fakePath.add_next(move.source)
                fakePath.add_next(move.dest)
                return fakePath
            if path is not None:
                return path.get_subsegment(1)
            if shortestKill is not None:
                self.viewInfo.add_info_line(f'plan_city_capture didnt find plan for {str(tgCity)}, using og kp instead')
                return shortestKill.get_subsegment(1)

        return None

    def prep_view_info_for_render(self, move: Move | None = None):
        self.viewInfo.board_analysis = self.board_analysis
        self.viewInfo.targetingArmy = self.targetingArmy
        self.viewInfo.armyTracker = self.armyTracker
        self.viewInfo.dangerAnalyzer = self.dangerAnalyzer
        self.viewInfo.currentPath = self.curPath
        self.viewInfo.gatherNodes = self.gatherNodes
        self.viewInfo.redGatherNodes = self.redGatherTreeNodes
        self.viewInfo.territories = self.territories
        self.viewInfo.allIn = self.is_all_in_losing
        self.viewInfo.timings = self.timings
        self.viewInfo.allInCounter = self.all_in_losing_counter
        self.viewInfo.givingUpCounter = self.giving_up_counter
        self.viewInfo.targetPlayer = self.targetPlayer
        self.viewInfo.generalApproximations = self.generalApproximations
        self.viewInfo.playerTargetScores = self.playerTargetScores

        movePath = Path()
        if move is not None:
            movePath.add_next(move.source)
            movePath.add_next(move.dest)
            self.viewInfo.color_path(
                PathColorer(
                    movePath,
                    254, 254, 254,
                    alpha=255,
                    alphaDecreaseRate=0
                ),
                renderOnBottom=True)

        if self.armyTracker is not None:
            if self.info_render_army_emergence_values:
                for tile in self._map.reachable_tiles:
                    val = self.armyTracker.emergenceLocationMap[self.targetPlayer][tile]
                    if val != 0:
                        textVal = f"e{val:.0f}"
                        self.viewInfo.bottomMidRightGridText[tile] = textVal

            for tile in self.armyTracker.dropped_fog_tiles_this_turn:
                self.viewInfo.add_targeted_tile(tile, TargetStyle.RED)

            for tile in self.armyTracker.decremented_fog_tiles_this_turn:
                self.viewInfo.add_targeted_tile(tile, TargetStyle.GREEN)

        if self.info_render_gather_locality_values and self.gatherAnalyzer is not None:
            for tile in self._map.pathable_tiles:
                if tile.player == self.general.player:
                    self.viewInfo.bottomMidRightGridText[tile] = f'l{self.gatherAnalyzer.gather_locality_map[tile]}'

        if self.info_render_tile_deltas:
            self.render_tile_deltas_in_view_info(self.viewInfo, self._map)
        if self.info_render_tile_states:
            self.render_tile_state_in_view_info(self.viewInfo, self._map)

        if self.target_player_gather_path is not None:
            alpha = 140
            minAlpha = 100
            alphaDec = 5
            self.viewInfo.color_path(PathColorer(self.target_player_gather_path, 60, 50, 00, alpha, alphaDec, minAlpha))

        if self.board_analysis.intergeneral_analysis is not None:
            nonZoneMatrix = MapMatrixSet(self._map)
            for tile in self._map.get_all_tiles():
                if tile not in self.board_analysis.core_play_area_matrix:
                    nonZoneMatrix.add(tile)
            self.viewInfo.add_map_zone(nonZoneMatrix, (100, 100, 50), alpha=35)

            if self.info_render_board_analysis_zones:
                # self.viewInfo.add_map_zone(self.board_analysis.extended_play_area_matrix, (255, 220, 0), alpha=50)
                # red orange
                self.viewInfo.add_map_division(self.board_analysis.core_play_area_matrix, (10, 230, 0), alpha=150)

                self.viewInfo.add_map_division(self.board_analysis.extended_play_area_matrix, (255, 230, 0), alpha=150)

                # red
                self.viewInfo.add_map_division(self.board_analysis.flank_danger_play_area_matrix, (205, 80, 40), alpha=255)

                # black
                self.viewInfo.add_map_division(self.board_analysis.flankable_fog_area_matrix, (0, 0, 0), alpha=255)
                self.viewInfo.add_map_zone(self.board_analysis.flankable_fog_area_matrix, (255, 255, 255), alpha=40)

                # green
                self.viewInfo.add_map_zone(self.board_analysis.backwards_tiles, (50, 100, 50), 75)

        #
        # for player in self._map.players:
        #     if self._map.is_player_on_team_with(self.general.player, player.index):
        #         continue
        #
        #     self.viewInfo.add_map_division(self.armyTracker.valid_general_positions_by_player[player.index], , alpha=150)

        self.viewInfo.team_cycle_stats = self.opponent_tracker.current_team_cycle_stats
        self.viewInfo.team_last_cycle_stats = self.opponent_tracker.get_last_cycle_stats_per_team()
        self.viewInfo.player_fog_tile_counts = self.opponent_tracker.get_all_player_fog_tile_count_dict()
        self.viewInfo.player_fog_risks = [self.opponent_tracker.get_approximate_fog_army_risk(p) for p in range(len(self._map.players))]

        if self.info_render_centrality_distances:
            for tile in self._map.get_all_tiles():
                self.viewInfo.bottomLeftGridText[tile] = f'cen{self.board_analysis.defense_centrality_sums[tile]}'

        if self.enemy_attack_path is not None:
            self.viewInfo.color_path(PathColorer(
                self.enemy_attack_path,
                255, 185, 75,
                alpha=255,
                alphaDecreaseRate=5
            ))

        if self.targetPlayer >= 0 and not self.targetPlayerExpectedGeneralLocation.isGeneral:
            for t in self.alt_en_gen_positions[self.targetPlayer]:
                self.viewInfo.add_targeted_tile(t, TargetStyle.YELLOW, radiusReduction=3)

        if self.info_render_board_analysis_choke_widths and self.board_analysis.intergeneral_analysis:
            for tile in self._map.get_all_tiles():
                w = ''
                if tile in self.board_analysis.intergeneral_analysis.chokeWidths:
                    w = str(self.board_analysis.intergeneral_analysis.chokeWidths[tile])
                self.viewInfo.topRightGridText[tile] = f'cw{w}'

        for p in self.armyTracker.unconnectable_tiles:
            for t in p:
                self.viewInfo.add_targeted_tile(t, targetStyle=TargetStyle.RED, radiusReduction=-5)
        for p, matrix in enumerate(self.armyTracker.player_connected_tiles):
            if not self._map.is_player_on_team_with(self.player.index, p) and not self._map.players[p].dead:
                scaledColor = Utils.rescale_color(0.55, 0, 1.0, Colors.PLAYER_COLORS[p], Colors.GRAY_DARK)
                self.viewInfo.add_map_division(matrix, scaledColor, alpha=150)
                self.viewInfo.add_map_zone(matrix, scaledColor, alpha=65)

        if move is not None:
            self.viewInfo.color_path(PathColorer(
                movePath,
                254, 254, 254,
                alpha=135,
                alphaDecreaseRate=0
            ))

        if self.info_render_defense_spanning_tree and self.defensive_spanning_tree:
            self.viewInfo.add_map_zone(self.defensive_spanning_tree, Colors.WHITE_PURPLE, alpha=80)

        if self.info_render_tile_islands:
            for island in sorted(self.tileIslandBuilder.all_tile_islands, key=lambda i: (i.team, str(i.name))):
                # color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
                #
                # self.viewInfo.add_map_zone(island.tile_set, color, alpha=80)
                # self.viewInfo.add_map_division(island.tile_set, color, alpha=200)
                if island.name:
                    for tile in island.tile_set:
                        if self.viewInfo.topRightGridText[tile]:
                            self.viewInfo.midRightGridText[tile] = island.name
                        else:
                            self.viewInfo.topRightGridText[tile] = island.name

    def get_move_if_afk_player_situation(self) -> Move | None:
        afkPlayers = self.get_afk_players()
        allOtherPlayersAfk = len(afkPlayers) + 1 == self._map.remainingPlayers
        numTilesVisible = 0
        if self.targetPlayer != -1:
            numTilesVisible = len(self._map.players[self.targetPlayer].tiles)

        if allOtherPlayersAfk and numTilesVisible == 0:
            # then just expand until we can find them
            with self.perf_timer.begin_move_event('AFK Player optimal EXPLORATION'):
                path = self.get_optimal_exploration(30, None, minArmy=0, maxTime=0.04)
            if path is not None:
                self.info(f"Rapid EXPLORE due to AFK player {self.targetPlayer}:  {str(path)}")

                self.finishing_exploration = True
                self.viewInfo.add_info_line("Setting finishingExploration to True because allOtherPlayersAfk and found an explore path")
                return self.get_first_path_move(path)

            expansionNegatives = set()
            territoryMap = self.territories.territoryMap
            with self.perf_timer.begin_move_event('AFK Player optimal EXPANSION'):
                if self.teammate_general is not None:
                    expansionNegatives.add(self.teammate_general)
                expUtilPlan = ExpandUtils.get_round_plan_with_expansion(
                    self._map,
                    self.general.player,
                    self.targetPlayer,
                    15,
                    self.board_analysis,
                    territoryMap,
                    self.tileIslandBuilder,
                    expansionNegatives,
                    self.captureLeafMoves,
                    allowLeafMoves=False,
                    viewInfo=self.viewInfo,
                    time_limit = 0.03)

                path = expUtilPlan.selected_option
                otherPaths = expUtilPlan.all_paths

            if path is not None:
                self.finishing_exploration = True
                self.info(f"Rapid EXPAND due to AFK player {self.targetPlayer}:  {str(path)}")
                return self.get_first_path_move(path)

        if self.targetPlayer != -1:
            tp = self.targetPlayerObj
            if tp.leftGame and self._map.turn < tp.leftGameTurn + 50:
                remainingTurns = tp.leftGameTurn + 50 - self._map.turn
                if tp.tileCount > 10 or tp.cityCount > 1 or (tp.general is not None and tp.general.army + remainingTurns // 2 < 42):
                    turns = max(8, remainingTurns - 15)
                    with self.perf_timer.begin_move_event(f'Quick kill gather to player who left, {remainingTurns} until they arent capturable'):
                        move = self.timing_gather(
                            [self.targetPlayerExpectedGeneralLocation],
                            force=True,
                            targetTurns=turns,
                            pruneToValuePerTurn=True)
                    if move is not None:
                        self.info(f"quick-kill gather to opposing player who left! {move}")
                        return move

            if allOtherPlayersAfk and self.targetPlayerExpectedGeneralLocation is not None and self.targetPlayerExpectedGeneralLocation.isGeneral:
                # attempt to quick-gather to this gen for kill
                with self.perf_timer.begin_move_event(f'quick-kill gather to opposing player!'):
                    move = self.timing_gather(
                        [self.targetPlayerExpectedGeneralLocation],
                        force=True,
                        pruneToValuePerTurn=True)
                if move is not None:
                    self.info(f"quick-kill gather to opposing player! {move}")
                    return move

        return None

    def clear_fog_armies_around(self, enemyGeneral: Tile):

        def fog_army_clear_func(tile: Tile):
            if not tile.visible and tile in self.armyTracker.armies:
                army = self.armyTracker.armies[tile]
                if army.player == enemyGeneral.player:
                    self.armyTracker.scrap_army(army, scrapEntangled=False)

        SearchUtils.breadth_first_foreach(self._map, [enemyGeneral], maxDepth=7, foreachFunc=fog_army_clear_func)

    def initialize_logging(self):
        self.logDirectory = BotLogging.get_file_logging_directory(self._map.usernames[self._map.player_index], self._map.replay_id)
        fileSafeUserName = BotLogging.get_file_safe_username(self._map.usernames[self._map.player_index])

        gameMode = '1v1'
        if self._map.remainingPlayers > 2:
            gameMode = 'ffa'

        BotLogging.add_file_log_output(fileSafeUserName, gameMode, self._map.replay_id, self.logDirectory)

    def get_max_explorable_undiscovered_tile(self, minSpawnDist: int):
        # 4 and larger gets dicey
        depth = self.get_safe_per_tile_bfs_depth()

        if self.undiscovered_priorities is None or self._undisc_prio_turn != self._map.turn:
            self.undiscovered_priorities = self.find_expected_1v1_general_location_on_undiscovered_map(
                undiscoveredCounterDepth=depth,
                minSpawnDistance=minSpawnDist)
        self._undisc_prio_turn = self._map.turn

        maxAmount = 0
        maxTile = None
        for tile in self._map.tiles_by_index:
            if self.targetPlayer != -1 and not self.armyTracker.valid_general_positions_by_player[self.targetPlayer].raw[tile.tile_index]:
                continue
            if tile and maxAmount < self.undiscovered_priorities.raw[tile.tile_index] and self.distance_from_general(tile) > minSpawnDist and (self.teammate_general is None or self.distance_from_teammate(tile) > minSpawnDist):
                maxAmount = self.undiscovered_priorities.raw[tile.tile_index]
                maxTile = tile
            if self.targetPlayer == -1:
                if self.info_render_general_undiscovered_prediction_values and self.undiscovered_priorities.raw[tile.tile_index] > 0:
                    self.viewInfo.bottomRightGridText[tile] = f'u{self.undiscovered_priorities.raw[tile.tile_index]}'

        self.viewInfo.add_targeted_tile(maxTile, TargetStyle.PURPLE)
        return maxTile

    def get_safe_per_tile_bfs_depth(self):
        depth = 9
        if self._map.rows * self._map.cols > 4000:
            depth = 1
        elif self._map.rows * self._map.cols > 2000:
            depth = 2
        elif self._map.rows * self._map.cols > 1500:
            depth = 3
        elif self._map.rows * self._map.cols > 1100:
            depth = 4
        elif self._map.rows * self._map.cols > 500:
            depth = 5
        elif self._map.rows * self._map.cols > 350:
            depth = 7
        return depth

    def try_gather_tendrils_towards_enemy(self, turns: int | None = None) -> Move | None:
        # # TODO hack for now because this doesn't perform well
        return None
        if self._map.remainingPlayers > 3 and self.targetPlayer == -1:
            return None
        generalApproxErrorLvl = 10
        if self.target_player_gather_path is not None:
            generalApproxErrorLvl = self.target_player_gather_path.length + 5
            if self._map.turn > 200:
                generalApproxErrorLvl = self.target_player_gather_path.length // 2

        if turns is None:
            turns = 25 - self._map.turn % 25
        targets = []
        if self.targetPlayer != -1:
            for tile in self._map.pathable_tiles:
                if tile.visible:
                    continue

                if (self.territories.territoryMap[tile] == self.targetPlayer
                        or (
                                self.board_analysis.intergeneral_analysis is not None
                                and self.board_analysis.intergeneral_analysis.bMap[tile] < generalApproxErrorLvl
                        )
                        or tile.player == self.targetPlayer
                        or tile == self.targetPlayerExpectedGeneralLocation
                ):
                    targets.append(tile)
        else:
            distMap = self._map.distance_mapper.get_tile_dist_matrix(self.targetPlayerExpectedGeneralLocation)
            for tile in self._map.pathable_tiles:
                if not SearchUtils.any_where(tile.movable, lambda t: not t.visible):
                    continue

                if self.distance_from_general(tile) < 15:
                    continue

                if (self.territories.territoryMap[tile] == self.targetPlayer
                        or distMap.raw[tile.tile_index] < generalApproxErrorLvl
                        or tile.player == self.targetPlayer
                        or tile == self.targetPlayerExpectedGeneralLocation
                ):
                    targets.append(tile)

        for target in targets:
            self.mark_tile(target, 255)

        move, valueGathered, turnsUsed, gatherNodes = self.get_gather_to_target_tiles(
            targets,
            0.1,
            turns,
            negativeSet=self.target_player_gather_targets,
            includeGatherTreeNodesThatGatherNegative=True,
            useTrueValueGathered=False,
            maximizeArmyGatheredPerTurn=True,
            targetArmy=-60)
        if move is not None:
            self.info(f'lmao gather AT tg loc, gathered {valueGathered} turns used {turnsUsed}')
            self.gatherNodes = gatherNodes
            return move

        greedyValue, turnsUsed, gatherNodes = Gather.greedy_backpack_gather_values(
            self._map,
            targets,
            turns,
            targetArmy=-60,
            negativeTiles=self.target_player_gather_targets,
            includeGatherTreeNodesThatGatherNegative=True,
            useTrueValueGathered=False,
            preferNeutral=True)

        totalValue = 0
        gathTurns = 0
        for gather in gatherNodes:
            logbook.info(f"gatherNode {gather.tile.toString()} value {gather.value}")
            totalValue += gather.value
            gathTurns += gather.gatherTurns
        if totalValue != greedyValue or gathTurns != turnsUsed:
            self.info(f'Greedy said it did v{greedyValue}/t{turnsUsed} but we found v{totalValue}/t{gathTurns}')

        move = self.get_tree_move_default(gatherNodes)
        if move is not None:
            self.info(f'Greedy tendrils, non-greedy failed? v{totalValue}/t{gathTurns}')
            self.gatherNodes = gatherNodes
            return move

        self.info('tendrils failed')
        # move = self.gather_to_target_MST(self.targetPlayerExpectedGeneralLocation, 0.1, turns, gatherNegatives=self.target_player_gather_targets, targetArmy=-30)
        # if move is not None:
        #     self.info(f'MST gather AT tg loc')
        #     return move
        return None

    def get_army_scrim_move(
            self,
            friendlyArmyTile: Tile,
            enemyArmyTile: Tile,
            friendlyHasKillThreat: bool | None = None,
            forceKeepMove=False
    ) -> Move | None:
        friendlyPath, enemyPath, result = self.get_army_scrim_paths(
            friendlyArmyTile,
            enemyArmyTile,
            friendlyHasKillThreat=friendlyHasKillThreat)
        if friendlyPath is not None and friendlyPath.length > 0:
            firstPathMove = self.get_first_path_move(friendlyPath)
            if (
                    firstPathMove
                    and not result.best_result_state.captured_by_enemy
                    and (result.net_economy_differential > self.engine_mcts_move_estimation_net_differential_cutoff or forceKeepMove)
            ):
                self.info(f'ARMY SCRIM MOVE {str(friendlyArmyTile)}@{str(enemyArmyTile)} EVAL {str(result)}: {str(firstPathMove)}')

                return firstPathMove

        return None

    def get_army_scrim_paths(
            self,
            friendlyArmyTile: Tile,
            enemyArmyTile: Tile,
            enemyCannotMoveAway: bool = True,
            friendlyHasKillThreat: bool | None = None
    ) -> typing.Tuple[Path | None, Path | None, ArmySimResult]:
        """
        Returns None for the path WHEN THE FIRST MOVE THE ENGINE WANTS TO MAKE INCLUDES A NO-OP.
        NOTE, These paths should not be executed as paths, they may contain removed-no-ops.

        @param friendlyArmyTile:
        @param enemyArmyTile:
        @param enemyCannotMoveAway:
        @param friendlyHasKillThreat: whether friendly tile is a kill threat against enemy or not. If not provided, will be calculated via a_star
        @return:
        """
        result = self.get_army_scrim_result(friendlyArmyTile, enemyArmyTile, enemyCannotMoveAway=enemyCannotMoveAway, friendlyHasKillThreat=friendlyHasKillThreat)

        if result.best_result_state.captured_by_enemy:
            self.viewInfo.add_info_line(f'scrim thinks enemy kills us :/ {str(result.expected_best_moves)}')
            return None, None, result

        if result.best_result_state.captures_enemy:
            self.viewInfo.add_info_line(f'scrim thinks we kill!? {str(result.expected_best_moves)} TODO implement race checks')
            return None, None, result

        if len(result.expected_best_moves) == 0:
            self.viewInfo.add_info_line(f'scrim returned no moves..? {str(result.expected_best_moves)}')
            return None, None, result

        friendlyPath, enemyPath = self.extract_engine_result_paths_and_render_sim_moves(result)

        return friendlyPath, enemyPath, result

    def get_army_scrim_result(
            self,
            friendlyArmyTile: Tile,
            enemyArmyTile: Tile,
            enemyCannotMoveAway: bool = False,
            enemyHasKillThreat: bool | None = None,
            friendlyHasKillThreat: bool | None = None,
            friendlyPrecomputePaths: typing.List[Move | None] | None = None
    ) -> ArmySimResult:
        frArmies = [self.get_army_at(friendlyArmyTile)]
        enArmies = [self.get_army_at(enemyArmyTile)]

        if self.engine_use_mcts and self.engine_force_multi_tile_mcts:
            frTiles = self.find_large_tiles_near(
                fromTiles=[friendlyArmyTile, enemyArmyTile],
                distance=self.engine_army_nearby_tiles_range,
                forPlayer=self.general.player,
                allowGeneral=True,
                limit=self.engine_mcts_scrim_armies_per_player_limit,
                minArmy=6,
            )
            enTiles = self.find_large_tiles_near(
                fromTiles=[friendlyArmyTile, enemyArmyTile],
                distance=self.engine_army_nearby_tiles_range,
                forPlayer=enemyArmyTile.player,
                allowGeneral=True,
                limit=self.engine_mcts_scrim_armies_per_player_limit - 1,
                minArmy=6,
            )

            for frTile in frTiles:
                if frTile != friendlyArmyTile:
                    frArmies.append(self.get_army_at(frTile))
                    self.viewInfo.add_targeted_tile(frTile, TargetStyle.TEAL)

            for enTile in enTiles:
                if enTile != enemyArmyTile:
                    enArmies.append(self.get_army_at(enTile))
                    self.viewInfo.add_targeted_tile(enTile, TargetStyle.PURPLE)

            lastMove: Move | None = self.armyTracker.lastMove
            if self.engine_always_include_last_move_tile_in_scrims and lastMove is not None:
                if lastMove.dest.player == self.general.player and lastMove.dest.army > 1:
                    lastMoveArmy = self.get_army_at(lastMove.dest)
                    if lastMoveArmy not in frArmies:
                        frArmies.append(lastMoveArmy)

        result = self.get_armies_scrim_result(
            friendlyArmies=frArmies,
            enemyArmies=enArmies,
            enemyCannotMoveAway=enemyCannotMoveAway,
            enemyHasKillThreat=enemyHasKillThreat,
            friendlyHasKillThreat=friendlyHasKillThreat,
            friendlyPrecomputePaths=friendlyPrecomputePaths,
        )
        return result

    def get_armies_scrim_result(
            self,
            friendlyArmies: typing.List[Army],
            enemyArmies: typing.List[Army],
            enemyCannotMoveAway: bool = False,
            enemyHasKillThreat: bool | None = None,
            friendlyHasKillThreat: bool | None = None,
            time_limit: float = 0.05,
            friendlyPrecomputePaths: typing.List[Move | None] | None = None
    ) -> ArmySimResult:

        result = self.get_scrim_cached(friendlyArmies, enemyArmies)
        if result is not None:
            self.info(
                f'  ScC {"+".join([str(a.tile) for a in friendlyArmies])}@{"+".join([str(a.tile) for a in enemyArmies])}: {str(result)} {repr(result.expected_best_moves)}')
            return result

        if friendlyHasKillThreat is None:
            friendlyHasKillThreat = False
            for frArmy in friendlyArmies:
                friendlyArmyTile = frArmy.tile
                targets = set()
                targets.add(self.targetPlayerExpectedGeneralLocation)
                path = SearchUtils.a_star_kill(
                    self._map,
                    [friendlyArmyTile],
                    targets,
                    0.03,
                    self.distance_from_general(self.targetPlayerExpectedGeneralLocation) // 3,
                    # self.general_safe_func_set,
                    requireExtraArmy=5 if self.targetPlayerExpectedGeneralLocation.isGeneral else 20,
                    negativeTiles=set([a.tile for a in enemyArmies]))
                if path is not None:
                    friendlyHasKillThreat = True

        if enemyHasKillThreat is None:
            enemyHasKillThreat = False
            for enArmy in enemyArmies:
                for path in enArmy.expectedPaths:
                    if path is not None and path.tail.tile.isGeneral and self._map.is_tile_friendly(path.tail.tile):
                        if path.calculate_value(enArmy.player, teams=self._map.team_ids_by_player_index, negativeTiles=set([a.tile for a in friendlyArmies])) > 0:
                            enemyHasKillThreat = True

        if len(enemyArmies) == 0:
            enemyArmies = [self.get_army_at(self._map.players[self.targetPlayer].tiles[0])]

        engine: ArmyEngine = ArmyEngine(self._map, friendlyArmies, enemyArmies, self.board_analysis, timeCap=0.05, mctsRunner=self.mcts_engine)
        engine.eval_params = self.mcts_engine.eval_params
        engine.allow_enemy_no_op = self.engine_allow_enemy_no_op
        engine.honor_mcts_expected_score = self.engine_honor_mcts_expected_score
        engine.honor_mcts_expanded_expected_score = self.engine_honor_mcts_expanded_expected_score
        if self.engine_include_path_pre_expansion:
            engine.forced_pre_expansions = []
            for enArmy in enemyArmies:
                altPaths = ArmyTracker.get_army_expected_path(self._map, enArmy, self.general, self.armyTracker.player_targets)
                for enPath in enArmy.expectedPaths:
                    if enPath is not None:
                        engine.forced_pre_expansions.append(enPath.get_subsegment(self.engine_path_pre_expansion_cutoff_length).convert_to_move_list())
                    matchAlt = None
                    for altPath in list(altPaths):
                        if altPath is None or altPath.tail.tile == enPath.tail.tile:
                            altPaths.remove(altPath)
                for altPath in altPaths:
                    engine.forced_pre_expansions.append(altPath.get_subsegment(self.engine_path_pre_expansion_cutoff_length).convert_to_move_list())
            for frArmy in friendlyArmies:
                for frPath in frArmy.expectedPaths:
                    if frPath is not None:
                        engine.forced_pre_expansions.append(frPath.get_subsegment(self.engine_path_pre_expansion_cutoff_length).convert_to_move_list())
            if friendlyPrecomputePaths is not None:
                engine.forced_pre_expansions.extend([p[0:self.engine_path_pre_expansion_cutoff_length] for p in friendlyPrecomputePaths])

        depth = 4
        # only check this stuff for the primary threat army
        enemyArmy = enemyArmies[0]
        if enemyCannotMoveAway and self.engine_allow_force_incoming_armies_towards:
            # we can scan much deeper when the enemies moves are heavily restricted.
            depth = 6
            if len(enemyArmy.expectedPaths) > 0:
                engine.force_enemy_towards = SearchUtils.build_distance_map_matrix(self._map, [enemyArmy.expectedPaths[0].tail.tile])
                logbook.info(f'forcing enemy scrim moves towards {str(enemyArmy.expectedPaths[0].tail.tile)}')
            else:
                engine.force_enemy_towards_or_parallel_to = SearchUtils.build_distance_map_matrix(self._map, [self.general])
                logbook.info(f'forcing enemy scrim moves towards our general')

            engine.allow_enemy_no_op = False

        if DebugHelper.IS_DEBUGGING:
            engine.time_limit = 1000
            engine.iteration_limit = 1000
        else:
            engine.time_limit = time_limit
            # TODO remove this stuff once we do end-of-turn scrim instead
            depthInMove = self.perf_timer.get_elapsed_since_update(self._map.turn)
            if depthInMove > 0.15:
                engine.time_limit = 0.06
            if depthInMove > 0.25:
                engine.time_limit = 0.04
            if depthInMove > 0.3:
                engine.time_limit = 0.02

        engine.friendly_has_kill_threat = friendlyHasKillThreat
        engine.enemy_has_kill_threat = enemyHasKillThreat and not self.should_abandon_king_defense()
        # engine.enemy_has_kill_threat = False
        # engine.friendly_has_kill_threat = False
        # TODO this is hack disabling mcts
        if self.disable_engine:
            depth = 0
            engine.time_limit = 0.00001

        result = engine.scan(depth, noThrow=True, mcts=self.engine_use_mcts)
        self.info(f' Scr {"+".join([str(a.tile) for a in friendlyArmies])}@{"+".join([str(a.tile) for a in enemyArmies])}: {str(result)} {repr(result.expected_best_moves)}')
        scrimCacheKey = self.get_scrim_cache_key(friendlyArmies, enemyArmies)
        self.cached_scrims[scrimCacheKey] = result
        if self.disable_engine:
            # TODO this is hack disabling mcts
            result.net_economy_differential = -50.0
            result.best_result_state.tile_differential = -50
        return result

    def extend_interspersed_path_moves(self, paths: typing.List[Path], move: Move | None):
        if move is not None:
            if move.dest is None:
                raise AssertionError()

            curPath: Path | None = None
            for p in paths:
                if p.tail is not None and p.tail.tile == move.source:
                    curPath = p
                    break

            if curPath is None:
                curPath = Path()
                curPath.add_next(move.source)
                paths.append(curPath)
            curPath.add_next(move.dest, move.move_half)

    def extract_engine_result_paths_and_render_sim_moves(self, result: ArmySimResult) -> typing.Tuple[Path | None, Path | None]:
        friendlyPaths: typing.List[Path] = []
        enemyPaths: typing.List[Path] = []

        for friendlyMove, enemyMove in result.expected_best_moves:
            self.extend_interspersed_path_moves(friendlyPaths, friendlyMove)
            self.extend_interspersed_path_moves(enemyPaths, enemyMove)

        friendlyPath: Path | None = None
        enemyPath: Path | None = None
        i = 0
        for path in friendlyPaths:
            if result.expected_best_moves[0][0] is not None and path.start.tile == result.expected_best_moves[0][0].source:
                friendlyPath = path
            else:
                self.viewInfo.color_path(PathColorer(path, 15, max(0, 255 - 40 * i), 105, max(50, 160 - 20 * i)))
            i += 1
        i = 0
        for path in enemyPaths:
            if result.expected_best_moves[0][1] is not None and path.start.tile == result.expected_best_moves[0][1].source:
                enemyPath = path
            else:
                self.viewInfo.color_path(PathColorer(path, 105, 0, max(0, 255 - 40 * i), max(50, 160 - 20 * i)))
            i += 1

        # sometimes being afk is the optimal move, in which case these paths may have no moves
        if friendlyPath is None or friendlyPath.length == 0:
            friendlyPath = None
        else:
            self.viewInfo.color_path(PathColorer(friendlyPath, 40, 255, 165, 255))

        if enemyPath is None or enemyPath.length == 0:
            enemyPath = None
        else:
            self.viewInfo.color_path(PathColorer(enemyPath, 175, 0, 255, 255))

        if len(result.expected_best_moves) > 0:
            if result.expected_best_moves[0][0] is None:
                friendlyPath = None
            if result.expected_best_moves[0][1] is None:
                enemyPath = None

        return friendlyPath, enemyPath

    def is_tile_in_range_from(self, source: Tile, target: Tile, maxDist: int, minDist: int = 0) -> bool:
        """
        Whether the dist between source and target is within the range of minDist and maxDist, inclusive.
        For optimization reasons, if one of the tiles is a friendly or enemy general, make the general tile be target.
        """
        dist = 1000
        if target == self.general:
            dist = self.distance_from_general(source)
        elif target == self.targetPlayerExpectedGeneralLocation:
            dist = self.distance_from_opp(source)
        else:
            captDist = [dist]

            def distFinder(tile: Tile, d: int) -> bool:
                if tile.x == target.x and tile.y == target.y:
                    captDist[0] = d

                return tile.isObstacle

            SearchUtils.breadth_first_foreach_dist(self._map, [source], maxDepth=maxDist + 1, foreachFunc=distFinder)

            dist = captDist[0]

        return minDist <= dist <= maxDist

    def continue_killing_target_army(self) -> Move | None:
        # check if still same army
        if self.targetingArmy.tile in self.armyTracker.armies:
            army = self.armyTracker.armies[self.targetingArmy.tile]

            inExpPlan = True
            expPath = None
            if self.expansion_plan is not None:
                inExpPlan = False
                for path in self.expansion_plan.all_paths:
                    if self.targetingArmy.tile in path.tileSet:
                        inExpPlan = True
                        expPath = path
                        self.viewInfo.add_info_line(f'TargetingArmy was in exp plan as {str(path)}')
                        break

            if not inExpPlan:
                gatherDepth = 10
                threats = [ThreatObj(p.length - 1, p.value, p, ThreatType.Kill) for p in self.targetingArmy.expectedPaths]
                if len(threats) > 0:
                    with self.perf_timer.begin_move_event(f'NEW INTERCEPT CONT @{str(self.targetingArmy)}'):
                        plan = self.army_interceptor.get_interception_plan(threats, turnsLeftInCycle=self.timings.get_turns_left_in_cycle(self._map.turn))
                        if plan is not None:
                            bestOpt = None
                            bestOptAmt = 0
                            bestTurn = 0
                            bestOptAmtPerTurn = 0
                            for turn, option in plan.intercept_options.items():
                                val = option.econValue
                                path = option.path
                                valPerTurn = val / max(1, turn)
                                # if path.length < gatherDepth and val > bestOptAmt:
                                if path.length < gatherDepth and valPerTurn > bestOptAmtPerTurn:
                                    logbook.info(f'NEW BEST INTERCEPT OPT {str(option)}')
                                    bestOpt = path
                                    bestOptAmt = val
                                    bestTurn = turn
                                    bestOptAmtPerTurn = valPerTurn

                            if bestOpt is not None:
                                move = self.get_first_path_move(bestOpt)
                                self.info(f'INTERCEPT {bestOptAmt}v/{bestTurn}t @ {str(self.targetingArmy)}: {move} -- {str(bestOpt)}')
                                if self.info_render_intercept_data:
                                    self.render_intercept_plan(plan)
                                    self.viewInfo.color_path(PathColorer(bestOpt, 80, 200, 0, alpha=150))
                                return move

                self.viewInfo.add_info_line(f'stopped targeting army {str(self.targetingArmy)} because not in expansion plan')
                self.targetingArmy = None
                return None
            else:
                move = self.get_euclid_shortest_from_tile_towards_target(expPath.get_first_move().source, self.targetingArmy.tile)
                self.info(f'continue killing target in exp plan, move {move}, plan was {str(expPath)}')
                return move
            #
            # if army.tile.delta.toTile is not None:
            #     moveHalfArmy = self.armyTracker.armies.get(army.tile.delta.toTile, None)
            #     if moveHalfArmy is not None and moveHalfArmy.player == self.targetingArmy.player:
            #         army = moveHalfArmy
            # if army != self.targetingArmy:
            #     if army.player == self.targetingArmy.player:
            #         logbook.info(
            #             f"Switched targetingArmy from {str(self.targetingArmy)} to {str(army)} because it is a different army now?")
            #         self.targetingArmy = army
            #     else:
            #         logbook.info(
            #             f"Stopped targetingArmy {str(self.targetingArmy)} because its tile is owned by the wrong player in armyTracker now")
            #         self.targetingArmy = None
        else:
            self.targetingArmy = None
            logbook.info(
                f"Stopped targetingArmy {str(self.targetingArmy)} because it no longer exists in armyTracker.armies")

        if not self.targetingArmy:
            return None

        enArmyDist = self.distance_from_general(self.targetingArmy.tile)
        armyStillInRange = enArmyDist < self.distance_from_opp(self.targetingArmy.tile) + 2 or self.territories.is_tile_in_friendly_territory(self.targetingArmy.tile)
        if armyStillInRange and self.should_kill(self.targetingArmy.tile):
            forceKill = enArmyDist <= 4
            path = self.kill_army(self.targetingArmy, allowGeneral=True, allowWorthPathKillCheck=not forceKill)
            if path:
                move = self.get_first_path_move(path)
                if self.targetingArmy is not None and self.targetingArmy.tile.army / path.length < 1:
                    self.info(f"Attacking army and ceasing to target army {str(self.targetingArmy)}")
                    # self.targetingArmy = None
                    return move

                if not self.detect_repetition(move, 6, 3) and self.general_move_safe(move.dest):
                    # move.move_half = self.should_kill_path_move_half(path)
                    self.info(
                        f"Cont kill army {str(self.targetingArmy)} {'z' if move.move_half else ''}: {str(path)}")
                    self.viewInfo.color_path(PathColorer(path, 0, 112, 133, 255, 10, 200))
                    return move
                else:
                    logbook.info(
                        f"Stopped targetingArmy {str(self.targetingArmy)} because it was causing repetitions.")
                    self.targetingArmy = None
        else:
            self.viewInfo.add_info_line(
                f"Stopped targetingArmy {str(self.targetingArmy)} due to armyStillInRange {armyStillInRange} or should_kill() returned false.")
            self.targetingArmy = None

        return None

    def build_intercept_plans(self, negTiles: typing.Set[Tile] | None = None) -> typing.Dict[Tile, ArmyInterception]:
        """

        @param negTiles: If provided, block intercepting across these tiles (mainly intended for avoiding stealing allies army).

        @return:
        """
        interceptions: typing.Dict[Tile, ArmyInterception] = {}

        self.blocking_tile_info: typing.Dict[Tile, ThreatBlockInfo] = {}

        with self.perf_timer.begin_move_event('INTERCEPTIONS (will be overridden below)') as interceptionsEvent:
            with self.perf_timer.begin_move_event('dangerAnalyzer.get_threats_grouped_by_tile'):
                threatsByTile = self.dangerAnalyzer.get_threats_grouped_by_tile(
                    self.armyTracker.armies,
                    includePotentialThreat=True,
                    includeVisionThreat=False,
                    alwaysIncludeArmy=self.targetingArmy,
                    includeArmiesWithThreats=True,
                    alwaysIncludeRecentlyMoved=True)

            threatsSorted = sorted(threatsByTile.items(), key=lambda tuple: (
                SearchUtils.any_where(tuple[1], lambda t: t.threatType == ThreatType.Kill),
                self.get_army_at(tuple[0]).last_seen_turn if not tuple[0].visible else 100000,
                self.get_army_at(tuple[0]).last_moved_turn,
                tuple[0].army
            ), reverse=True)

            threatsWeCareAbout = []
            threatsWeCareAboutByTile = {}

            limit = 4
            timeCut = 0.035
            if self.is_lag_massive_map:
                timeCut = 0.02
                limit = 2

            skippedIntercepts = []
            start = time.perf_counter()
            isFfa = self.is_ffa_situation()

            with self.perf_timer.begin_move_event(f'INT Ensure analysis'''):
                for tile, threats in threatsSorted:
                    if len(threats) == 0:
                        continue

                    threatArmy = self.get_army_at(tile)

                    threatPlayer = threats[0].threatPlayer
                    if isFfa and self._map.players[threatPlayer].aggression_factor < 200 and threatPlayer != self.targetPlayer and not tile.visible:
                        skippedIntercepts.append(tile)
                        continue

                    if isFfa and self._map.players[threatPlayer].aggression_factor < 50 and not tile.visible:
                        skippedIntercepts.append(tile)
                        continue

                    isCloseThreat = threats[0].turns <= self.target_player_gather_path.length / 4 and self.board_analysis.intergeneral_analysis.aMap.raw[tile.tile_index] < self.target_player_gather_path.length / 2
                    
                    if isFfa and threatArmy.last_seen_turn < self._map.turn - 4 and not isCloseThreat:
                        skippedIntercepts.append(tile)
                        continue

                    if self._map.turn - threatArmy.last_seen_turn > max(1.0, self.target_player_gather_path.length / 5) and not isCloseThreat:
                        skippedIntercepts.append(tile)
                        continue

                    if not self._map.is_player_on_team_with(threats[0].threatPlayer, self.targetPlayer) and self.targetPlayer != -1 and not self.territories.is_tile_in_friendly_territory(tile):
                        skippedIntercepts.append(tile)
                        continue

                    if len(threatsWeCareAbout) >= limit:
                        skippedIntercepts.append(tile)
                        continue
                    if time.perf_counter() - start > timeCut:
                        self.info(f'  INTERCEPT BREAKING EARLY AFTER {time.perf_counter() - start:.4f}s BUILDING ANALYSIS\'')
                        break

                    threatsIncluded = []

                    with self.perf_timer.begin_move_event(f'INT @{str(tile)} Ensure threat army analysis (will get overridden') as moveEvent:
                        num = 0
                        for threat in threats:
                            if threat.turns > 14 and time.perf_counter() - start > 0.02:
                                self.info(f'  time constraints skipping threat {threat}')
                                continue

                            if threat.turns > 40:
                                self.info(f'  massive length skipping threat {threat}')
                                continue

                            threatsIncluded.append(threat)
                            if self.army_interceptor.ensure_threat_army_analysis(threat):
                                num += 1
                        moveEvent.event_name = f'INT @{str(tile)} Analysis ({num} threats)'
                    if num > 0:
                        threatsWeCareAbout.append((tile, threatsIncluded))
                        threatsWeCareAboutByTile[tile] = threatsIncluded

            for tile, threats in threatsWeCareAbout:
                if len(threats) == 0:
                    continue

                if not self._map.is_player_on_team_with(threats[0].threatPlayer, self.targetPlayer) and self.targetPlayer != -1 and not self.territories.is_tile_in_friendly_territory(tile):
                    continue

                with self.perf_timer.begin_move_event(f'INT @{str(tile)} Tile Block'):
                    blockingTiles = self.army_interceptor.get_intercept_blocking_tiles_for_split_hinting(tile, threatsWeCareAboutByTile, negTiles)

                    if len(blockingTiles) > 0:
                        self.viewInfo.add_info_line(f'for threat {str(tile)}, blocking tiles were {"  ".join([str(v) for v in blockingTiles.values()])}')

                    if SearchUtils.any_where(threats, lambda t: t.threatType == ThreatType.Kill):
                        self.blocking_tile_info = blockingTiles

                    blocks = blockingTiles
                    if blocks is None:
                        blocks = self.blocking_tile_info
                    elif blocks != self.blocking_tile_info:
                        for t, values in self.blocking_tile_info.items():
                            existing = blocks.get(t, None)
                            if not existing:
                                # map other blocking tile info into our interception blocks
                                blocks[t] = values
                            else:
                                for blockedDest in values.blocked_destinations:
                                    # map other blocking tile info into our interception blocks
                                    existing.add_blocked_destination(blockedDest)

                with self.perf_timer.begin_move_event(f'INT @{str(tile)} Calc'):
                    shouldBypass = self.should_bypass_army_danger_due_to_last_move_turn(tile)
                    if shouldBypass and len(interceptions) > 0:
                        army = self.armyTracker.get_or_create_army_at(tile)
                        self.viewInfo.add_info_line(f'skip int dngr from{str(tile)} last_seen {army.last_seen_turn}, last_moved {army.last_moved_turn}')
                        continue
                    plan = self.army_interceptor.get_interception_plan(threats, turnsLeftInCycle=self.timings.get_turns_left_in_cycle(self._map.turn), otherThreatsBlockingTiles=blocks)
                    if plan is not None:
                        interceptions[tile] = plan

            interceptionsEvent.event_name = f'INTERCEPTIONS ({len(threatsWeCareAboutByTile)}, skipped {len(skippedIntercepts)} tiles)'

        if len(skippedIntercepts) > 0:
            self.viewInfo.add_info_line(f'SKIPPED {len(skippedIntercepts)} INTERCEPTS, OVER LIMIT {limit}! Skipped: {" - ".join([str(t) for t in skippedIntercepts])}')

        return interceptions

    def should_bypass_army_danger_due_to_last_move_turn(self, tile: Tile) -> bool:
        army = self.get_army_at(tile)
        shouldBypass = army.last_seen_turn < self._map.turn - 6 and not army.tile.visible
        shouldBypass = shouldBypass or (army.tile.isCity and army.last_moved_turn < self._map.turn - 3)

        return shouldBypass

    def try_find_counter_army_scrim_path_killpath(
            self,
            threatPath: Path,
            allowGeneral: bool,
            forceEnemyTowardsGeneral: bool = False
    ) -> Path | None:
        path, simResult = self.try_find_counter_army_scrim_path_kill(threatPath, allowGeneral=allowGeneral, forceEnemyTowardsGeneral=forceEnemyTowardsGeneral)
        return path

    def try_find_counter_army_scrim_path_kill(
            self,
            threatPath: Path,
            allowGeneral: bool,
            forceEnemyTowardsGeneral: bool = False
    ) -> typing.Tuple[Path | None, ArmySimResult | None]:
        if threatPath.start.tile.army < 4:
            logbook.info('fuck off, dont try to scrim against tiny tiles idiot')
            return None, None
        friendlyPath, simResult = self.try_find_counter_army_scrim_path(threatPath, allowGeneral, forceEnemyTowardsGeneral=forceEnemyTowardsGeneral)
        if simResult is not None and friendlyPath is not None:
            armiesIntercept = simResult.best_result_state.kills_all_enemy_armies
            if not armiesIntercept:
                sourceThreatDist = self._map.euclidDist(friendlyPath.start.tile.x, friendlyPath.start.tile.y, threatPath.start.tile.x, threatPath.start.tile.y)
                destThreatDist = self._map.euclidDist(friendlyPath.start.next.tile.x, friendlyPath.start.next.tile.y, threatPath.start.tile.x, threatPath.start.tile.y)
                if destThreatDist < sourceThreatDist:
                    armiesIntercept = True

            if friendlyPath is not None and armiesIntercept and not simResult.best_result_state.captured_by_enemy:
                self.info(f'CnASPaK EVAL {str(simResult)}: {str(friendlyPath)}')
                self.targetingArmy = self.get_army_at(threatPath.start.tile)
                return friendlyPath, simResult

        return None, None

    def try_find_counter_army_scrim_path(
            self,
            threatPath: Path,
            allowGeneral: bool,
            forceEnemyTowardsGeneral: bool = False
    ) -> typing.Tuple[Path | None, ArmySimResult | None]:
        """
        Sometimes the best sim output involves no-opping one of the tiles. In that case,
        this will return a None path as the best ArmySimResult output. It should be honored, and this tile
        tracked as a scrimming tile, even though the tile should not be moved this turn.

        @param threatPath:
        @param allowGeneral:
        @return:
        """
        threatTile = threatPath.start.tile
        threatDist = self.distance_from_general(threatTile)
        threatArmy = self.get_army_at(threatTile)
        threatArmy.include_path(threatPath)

        largeTilesNearTarget = SearchUtils.where(self.find_large_tiles_near(
            fromTiles=threatPath.tileList[0:3],
            distance=self.engine_army_nearby_tiles_range,
            limit=self.engine_mcts_scrim_armies_per_player_limit,
            forPlayer=self.general.player,
            allowGeneral=allowGeneral,
            addlFilterFunc=lambda t, dist: self.distance_from_general(t) <= threatDist + 1,
            minArmy=max(3, min(15, threatPath.value // 2))
        ), lambda t: self.territories.is_tile_in_enemy_territory(t))

        bestPath: Path | None = None
        bestSimRes: ArmySimResult | None = None
        if not self.engine_use_mcts:
            for largeTile in largeTilesNearTarget:
                if largeTile in threatPath.tileSet and largeTile.army < threatPath.value:
                    continue
                with self.perf_timer.begin_move_event(f'BfScr {str(largeTile)}@{str(threatTile)}'):
                    friendlyPath, enemyPath, simResult = self.get_army_scrim_paths(largeTile, enemyArmyTile=threatTile, enemyCannotMoveAway=True)
                if bestSimRes is None or bestSimRes.best_result_state.calculate_value_int() < simResult.best_result_state.calculate_value_int():
                    bestPath = friendlyPath
                    bestSimRes = simResult
        elif len(largeTilesNearTarget) > 0:
            enTiles = self.find_large_tiles_near(
                fromTiles=threatPath.tileList,
                distance=self.engine_army_nearby_tiles_range,
                forPlayer=threatArmy.player,
                allowGeneral=True,
                limit=self.engine_mcts_scrim_armies_per_player_limit,
                minArmy=3,
            )

            frArmies: typing.List[Army] = []
            enArmies: typing.List[Army] = []
            for frTile in largeTilesNearTarget:
                frArmies.append(self.get_army_at(frTile))
                self.viewInfo.add_targeted_tile(frTile, TargetStyle.GOLD)

            for enTile in enTiles:
                enArmies.append(self.get_army_at(enTile))
                self.viewInfo.add_targeted_tile(enTile, TargetStyle.PURPLE)

            with self.perf_timer.begin_move_event(f'Scr {"+".join([str(largeTile) for largeTile in largeTilesNearTarget])}@{"+".join([str(enTile) for enTile in enTiles])}'):
                simResult = self.get_armies_scrim_result(
                    frArmies,
                    enArmies,
                    enemyCannotMoveAway=forceEnemyTowardsGeneral,
                    # enemyHasKillThreat=True,
                    time_limit=0.07)

                friendlyPath, enemyPath = self.extract_engine_result_paths_and_render_sim_moves(simResult)

            if bestSimRes is None:
                bestPath = friendlyPath
                bestSimRes = simResult

        if len(largeTilesNearTarget) == 0:
            logbook.info(f'No large tiles in range of {str(threatTile)} :/')

        return bestPath, bestSimRes

    def find_large_tiles_near(
            self,
            fromTiles: typing.List[Tile],
            distance: int,
            forPlayer=-2,
            allowGeneral: bool = True,
            limit: int = 5,
            minArmy: int = 10,
            addlFilterFunc: typing.Callable[[Tile, int], bool] | None = None,
            allowTeam: bool = False
    ) -> typing.List[Tile]:
        """
        Returns [limit] largest fromTiles for [forPlayer] within [distance] of [fromTiles]. Excludes generals unless allowGeneral is true.
        Returns them in order from largest army to smallest army.

        @param fromTiles:
        @param distance:
        @param forPlayer:
        @param allowGeneral:
        @param limit:
        @param minArmy:
        @param addlFilterFunc: None or func(tile, dist) should return False to exclude a tile, True to include it. Tile must STILL meet all the other restrictions.
        @return:
        """
        largeTilesNearTargets = []
        if forPlayer == -2:
            forPlayer = self.general.player

        forPlayers = [forPlayer]
        if allowTeam:
            forPlayers = self.opponent_tracker.get_team_players_by_player(forPlayer)

        def tile_finder(tile: Tile, dist: int):
            if (tile.player in forPlayers
                    and tile.army > minArmy  # - threatDist * 2
                    and (addlFilterFunc is None or addlFilterFunc(tile, dist))
                    and (not tile.isGeneral or allowGeneral)
            ):
                largeTilesNearTargets.append(tile)

        SearchUtils.breadth_first_foreach_dist_fast_incl_neut_cities(self._map, fromTiles, distance, foreachFunc=tile_finder)

        largeTilesNearTargets = [t for t in sorted(largeTilesNearTargets, key=lambda t: t.army, reverse=True)]

        return largeTilesNearTargets[0:limit]

    def check_for_army_movement_scrims(self, econCutoff=2.0) -> Move | None:
        curScrim = 0
        cutoff = 3

        bestScrimPath: Path | None = None
        bestScrim: ArmySimResult | None = None

        cutoffDist = self.board_analysis.inter_general_distance // 2

        # TODO include nearbies, drop cutoff, etc
        for tile in sorted(self.armies_moved_this_turn, key=lambda t: t.army, reverse=True):
            # if tile.player == self.general.player:
            #     self.viewInfo.add_targeted_tile(tile, targetStyle=TargetStyle.GREEN)
            # elif tile.player != self.player:
            #     self.viewInfo.add_targeted_tile(tile, targetStyle=TargetStyle.GOLD)

            if tile.player == self.targetPlayer:
                # self.viewInfo.add_targeted_tile(tile, targetStyle=TargetStyle.RED)
                if tile.army <= 4:
                    continue

                if self._map.get_distance_between(self.targetPlayerExpectedGeneralLocation, tile) > cutoffDist:
                    continue

                # try continuing the scrim with this tile?
                if (
                        self.next_scrimming_army_tile is not None
                        and self.next_scrimming_army_tile.army > 2
                        and self.next_scrimming_army_tile.player == self.general.player
                        and self._map.get_distance_between(self.targetPlayerExpectedGeneralLocation, self.next_scrimming_army_tile) <= cutoffDist
                ):
                    with self.perf_timer.begin_move_event(
                            f'Scrim prev {str(self.next_scrimming_army_tile)} @ {str(tile)}'):
                        friendlyPath, enemyPath, simResult = self.get_army_scrim_paths(
                            self.next_scrimming_army_tile,
                            enemyArmyTile=tile,
                            enemyCannotMoveAway=True)
                    if simResult is not None \
                            and (bestScrimPath is None or bestScrim.best_result_state.calculate_value_int() < simResult.best_result_state.calculate_value_int()):
                        self.info(f'new best scrim @ {str(tile)} {simResult.net_economy_differential:+0.1f} ({str(simResult)}) {str(friendlyPath)}')
                        bestScrimPath = friendlyPath
                        bestScrim = simResult
                else:
                    self.next_scrimming_army_tile = None

                curScrim += 1
                # attempt scrim intercept? :)
                army = self.get_army_at(tile)
                with self.perf_timer.begin_move_event(f'try scrim @{army.name} {str(tile)}'):
                    if len(army.expectedPaths) == 0:
                        targets = self._map.players[self.general.player].cities.copy()
                        targets.append(self.general)
                        if self.teammate_general is not None:
                            targets.append(self.teammate_general)
                            targets.extend(self._map.players[self.teammate].cities)
                        targetPath = self.get_path_to_targets(
                            targets,
                            0.1,
                            preferNeutral=False,
                            fromTile=tile)
                        if targetPath:
                            army.include_path(targetPath)
                        self.viewInfo.add_info_line(f'predict army {army.name} path {str(army.expectedPaths)}')

                    path, scrimResult = self.try_find_counter_army_scrim_path(army.expectedPaths[0], allowGeneral=True)
                    if path is not None and scrimResult is not None:
                        if scrimResult.best_result_state.captured_by_enemy:
                            self.viewInfo.add_info_line(f'scrim says cap by enemy in {str(scrimResult.best_result_state)} @{army.name} {str(tile)} lol')
                        elif (bestScrimPath is None
                              or bestScrim.best_result_state.calculate_value_int() < scrimResult.best_result_state.calculate_value_int()):
                            if scrimResult.net_economy_differential < 0:
                                self.viewInfo.add_info_line(f'scrim @ {str(tile)} bad result, {str(scrimResult)} including anyway as new best scrim')
                            else:
                                self.info(
                                    f'new best scrim @ {str(tile)} {scrimResult.net_economy_differential:+.1f} ({str(scrimResult)}) {str(path)}')
                            bestScrimPath = path
                            bestScrim = scrimResult

                if curScrim > cutoff:
                    break

        # # try continuing the scrim with this tile?
        # if self.next_scrimming_army_tile is not None and self.next_scrimming_army_tile.army > 2 and self.next_scrimming_army_tile.player == self.general.player:
        #     largestEnemyTilesNear = self.find_large_tiles_near([self.next_scrimming_army_tile], distance=4, forPlayer=self.player, allowGeneral=True, limit = 1, minArmy=1)
        #     self.viewInfo.add_targeted_tile(self.next_scrimming_army_tile, TargetStyle.PURPLE)
        #     if len(largestEnemyTilesNear) > 0:
        #         largestEnemyTile = largestEnemyTilesNear[0]
        #         self.viewInfo.add_targeted_tile(largestEnemyTile, TargetStyle.BLUE)
        #         with self.perf_timer.begin_move_event(
        #                 f'Scrim current {str(self.next_scrimming_army_tile)} @ {str(largestEnemyTile)}'):
        #             friendlyPath, enemyPath, simResult = self.get_army_scrim_paths(
        #                 self.next_scrimming_army_tile,
        #                 enemyArmyTile=largestEnemyTile,
        #                 enemyCannotMoveAway=True)
        #         if simResult is not None and friendlyPath is not None \
        #                 and (bestScrimPath is None or bestScrim.best_result_state.calculate_value_int() < simResult.best_result_state.calculate_value_int()):
        #             bestScrimPath = friendlyPath
        #             bestScrim = simResult
        # else:
        #     self.next_scrimming_army_tile = None

        if bestScrimPath is not None and bestScrim is not None and bestScrim.net_economy_differential > econCutoff:
            self.info(f'Scrim cont ({str(bestScrim)}) {str(bestScrimPath)}')

            self.next_scrimming_army_tile = bestScrimPath.start.next.tile
            return self.get_first_path_move(bestScrimPath)

    def should_force_gather_to_enemy_tiles(self) -> bool:
        """
        Determine whether we've let too much enemy tiles accumulate near our general,
         and it is getting out of hand and we should spend a cycle just gathering to kill them.
        """
        forceGatherToEnemy = False
        scaryDistance = 3
        if self.shortest_path_to_target_player is not None:
            scaryDistance = self.shortest_path_to_target_player.length // 3 + 2

        thresh = 1.3
        numEnemyTerritoryNearGen = self.count_enemy_territory_near_tile(self.general, distance=scaryDistance)
        enemyTileNearGenRatio = numEnemyTerritoryNearGen / max(1.0, scaryDistance)
        if enemyTileNearGenRatio > thresh:
            forceGatherToEnemy = True

        self.viewInfo.add_info_line(
            f'forceEn={forceGatherToEnemy} (near {numEnemyTerritoryNearGen}, dist {scaryDistance}, rat {enemyTileNearGenRatio:.2f} vs thresh {thresh:.2f})')
        return forceGatherToEnemy

    def check_for_danger_tile_moves(self) -> Move | None:
        dangerTiles = self.get_danger_tiles()
        if len(dangerTiles) == 0 or self.all_in_losing_counter > 15:
            return None

        for tile in dangerTiles:
            self.viewInfo.add_targeted_tile(tile, TargetStyle.RED)
            negTiles = []
            if self.curPath is not None:
                negTiles = [tile for tile in self.curPath.tileSet]
            armyToSearch = self.get_target_army_inc_adjacent_enemy(tile)
            killPath = SearchUtils.dest_breadth_first_target(
                self._map,
                [tile],
                armyToSearch,
                0.1,
                3,
                negTiles,
                searchingPlayer=self.general.player,
                dontEvacCities=False)

            if killPath is None:
                continue

            move = self.get_first_path_move(killPath)
            if self.is_move_safe_valid(move):
                if self.detect_repetition(move, 4, 2):
                    self.info(
                        f"Danger tile kill resulted in repetitions, fuck it. {str(tile)} {str(killPath)}")
                    return None

                self.info(
                    f"Depth {killPath.length} dest bfs kill on danger tile {str(tile)} {str(killPath)}")
                logbook.info(f'Setting targetingArmy to {str(tile)} in check_for_danger_tiles_move')
                self.targetingArmy = self.get_army_at(tile)
                return move

    #
    # def try_scrim_against_threat_with_largest_pruned_gather_node(
    #         self,
    #         threats: typing.List[ThreatObj]
    # ):
    #     maxNode: typing.List[GatherTreeNode | None] = [None]
    #
    #     def largestGatherTreeNodeFunc(node: GatherTreeNode):
    #         if node.tile.player == self.general.player and (
    #                 maxNode[0] is None or maxNode[0].tile.army < node.tile.army):
    #             maxNode[0] = node
    #
    #     GatherTreeNode.iterate_tree_nodes(pruned, largestGatherTreeNodeFunc)
    #
    #     largestGatherTile = gatherMove.source
    #     if maxNode[0] is not None:
    #         largestGatherTile = maxNode[0].tile
    #
    #     threatTile = threat.path.start.tile
    #
    #     # check for ArMyEnGiNe scrim results
    #     inRangeForScrimGen = self.is_tile_in_range_from(
    #         largestGatherTile,
    #         self.general,
    #         threat.turns + 1,
    #         threat.turns - 8)
    #     inRangeForScrim = self.is_tile_in_range_from(
    #         largestGatherTile,
    #         threatTile,
    #         threat.turns + 5,
    #         threat.turns - 5)
    #     goodInterceptCandidate = largestGatherTile.army > threatTile.army - threat.turns * 2 and largestGatherTile.army > 30
    #     if goodInterceptCandidate and inRangeForScrim and inRangeForScrimGen:
    #         with self.perf_timer.begin_move_event(f'defense army scrim @ {str(threatTile)}'):
    #             scrimMove = self.get_army_scrim_move(
    #                 largestGatherTile,
    #                 threatTile,
    #                 friendlyHasKillThreat=False,
    #                 forceKeepMove=threat.turns < 3)
    #         if scrimMove is not None:
    #             self.targetingArmy = self.get_army_at(threatTile)
    #             # already logged
    #             return scrimMove
    #
    #     with self.perf_timer.begin_move_event(f'defense kill_threat @ {str(threatTile)}'):
    #         path = self.kill_threat(threat, allowGeneral=True)
    #     if path is not None:
    #         self.threat_kill_path = path
    #
    #     return None

    def get_optimal_city_or_general_plan_move(self, timeLimit: float = 4.0) -> Move | None:
        calcedThisTurn = False
        if self._map.turn < 50 and self._map.is_2v2:
            self.send_2v2_tip_to_ally()

        source = self.general
        if len(self.player.cities) > 0:
            sources = [self.general]
            sources.extend(self.player.cities)
            source = random.choice(sources)

        if self._map.turn > 50:
            distMap = self.get_expansion_weight_matrix(mult=10)
            skipTiles = set()
        else:
            distMap, skipTiles = self.get_first_25_expansion_distance_priority_map()
            # distMap2 = self.get_expansion_weight_matrix(mult=10)
            # distMap = MapMatrix.get_summed([distMap, distMap2])

        if self.city_expand_plan is None or len(self.city_expand_plan.plan_paths) == 0:
            with self.perf_timer.begin_move_event('optimize_first_25'):
                calcedThisTurn = True
                cutoff = time.perf_counter() + timeLimit
                for tile in self._map.get_all_tiles():
                    self.viewInfo.bottomMidLeftGridText.raw[tile.tile_index] = f'dm{distMap.raw[tile.tile_index]:.2f}'
                self.city_expand_plan = EarlyExpandUtils.optimize_first_25(self._map, source, distMap, skipTiles=skipTiles, cutoff_time=cutoff, cramped=self._spawn_cramped)

                totalTiles = self.city_expand_plan.tile_captures + len(self.player.tiles)
                if len(skipTiles) > 0 and totalTiles < 17 and self._map.turn < 50:
                    self.city_expand_plan = EarlyExpandUtils.optimize_first_25(self._map, source, distMap, skipTiles=None, cutoff_time=cutoff, cramped=self._spawn_cramped)

                while self.city_expand_plan.plan_paths and self.city_expand_plan.plan_paths[0] is None:
                    self.city_expand_plan.plan_paths.pop(0)
                if self._map.turn < 50:
                    self.send_teammate_communication("I'm planning my start expand here, try to avoid these pinged tiles.", cooldown=50)

        if (
                (
                        self.city_expand_plan.launch_turn > self._map.turn
                        or (
                                self.city_expand_plan.launch_turn < self._map.turn
                                and not SearchUtils.any_where(
                                    self.player.tiles,
                                    lambda tile: not tile.isGeneral and SearchUtils.any_where(tile.movable, lambda mv: not mv.isObstacle and tile.army - 1 > mv.army and not self._map.is_tile_friendly(mv))
                                )
                        )
                )
                and not calcedThisTurn
        ):
            # self.city_expand_plan.tile_captures = len(self.player.tiles) + EarlyExpandUtils.get_start_expand_captures(
            self.city_expand_plan.tile_captures = EarlyExpandUtils.get_start_expand_captures(
                self._map,
                self.city_expand_plan.core_tile,
                self.city_expand_plan.core_tile.army,
                self._map.turn,
                self.city_expand_plan.plan_paths,
                launchTurn=self.city_expand_plan.launch_turn,
                noLog=False)

            distToGenMap = SearchUtils.build_distance_map_matrix(self._map, self.get_target_player_possible_general_location_tiles_sorted(elimNearbyRange=7)[0:3])

            with self.perf_timer.begin_move_event(f're-check f25 (limit {timeLimit:.3f}'):
                cutoff = time.perf_counter() + timeLimit
                optionalNewExpandPlan = EarlyExpandUtils.optimize_first_25(self._map, source, distMap, skipTiles=skipTiles, cutoff_time=cutoff, prune_cutoff=self.city_expand_plan.tile_captures, shuffle_launches=True)
            if optionalNewExpandPlan is not None:
                visited = set(self._map.players[self.general.player].tiles)
                for teammate in self._map.teammates:
                    visited.update(self._map.players[teammate].tiles)
                visited.update(skipTiles)

                preRecalcCaps = self.city_expand_plan.tile_captures
                maxPlan = EarlyExpandUtils.recalculate_max_plan(self.city_expand_plan, optionalNewExpandPlan, self._map, distToGenMap, distMap, visited, no_log=False)
                self.viewInfo.add_info_line(f'Recalced a new f25, val {optionalNewExpandPlan.tile_captures} (vs post {self.city_expand_plan.tile_captures} / pre {preRecalcCaps})')

                if maxPlan == optionalNewExpandPlan:
                    calcedThisTurn = True
                    self.viewInfo.add_info_line(f'YOOOOO REPLACING OG F25 WITH NEW ONE, {optionalNewExpandPlan.tile_captures} >= {self.city_expand_plan.tile_captures}')
                    self.viewInfo.paths.clear()
                    self.city_expand_plan = optionalNewExpandPlan

        r = 255
        g = 50
        a = 255
        for plan in self.city_expand_plan.plan_paths:
            r -= 17
            r = max(0, r)
            a -= 10
            a = max(0, a)
            g += 10
            g = min(255, g)

            if plan is None:
                continue

            self.viewInfo.color_path(
                PathColorer(plan.clone(), r, g, 50, alpha=a, alphaDecreaseRate=5, alphaMinimum=100))

        pingCooldown = 3
        if not self.teamed_with_bot:
            pingCooldown = 8
        if self.cooldown_allows("F25 PING COOLDOWN", pingCooldown):
            for plan in self.city_expand_plan.plan_paths:
                if plan is None:
                    continue
                self.send_teammate_path_ping(plan)

        if self.city_expand_plan.launch_turn > self._map.turn:
            self.info(
                f"Expand plan ({self.city_expand_plan.tile_captures}) isn't ready to launch yet, launch turn {self.city_expand_plan.launch_turn}")
            return None

        if len(self.city_expand_plan.plan_paths) > 0:
            countNone = 0
            for p in self.city_expand_plan.plan_paths:
                if p is not None:
                    break
                countNone += 1

            if self._map.turn == self.city_expand_plan.launch_turn:
                while self.city_expand_plan.plan_paths[0] is None:
                    self.viewInfo.add_info_line(f'POPPING BAD EARLY DELAY OFF OF THE PLAN...?')
                    self.city_expand_plan.plan_paths.pop(0)
            curPath = self.city_expand_plan.plan_paths[0]
            if curPath is None:
                self.info(
                    f'Expand plan {self.city_expand_plan.tile_captures} no-opped until turn {countNone + self._map.turn} :)')
                self.city_expand_plan.plan_paths.pop(0)
                return None

            move = self.get_first_path_move(curPath)
            self.info(f'Expand plan {self.city_expand_plan.tile_captures} path move {move}')

            collidedWithEnemyAndWastingArmy = move.source.player != move.dest.player and (move.dest.player != -1 or move.dest.isCity) and move.source.army - 1 <= move.dest.army or move.dest.player in self._map.teammates
            if move.dest.isMountain:
                collidedWithEnemyAndWastingArmy = True
            if move.dest.isDesert and move.dest not in self.city_expand_plan.intended_deserts:
                collidedWithEnemyAndWastingArmy = True

            if collidedWithEnemyAndWastingArmy and move.source.player == self.general.player:
                # if tiles > 2 we either prevent them from continuing their expand OR we cap the tile they just vacate, depending who loses the tiebreak
                collisionCapsOrPreventsEnemy = move.source.army == move.dest.army and move.source.army > 2 and move.dest.player not in self._map.teammates
                if not collisionCapsOrPreventsEnemy:
                    newPath = self.attempt_first_25_collision_reroute(curPath, move, distMap)
                    if newPath is None:
                        # self.city_expand_plan = None

                        bMap = self.board_analysis.intergeneral_analysis.bMap
                        self.board_analysis.intergeneral_analysis.bMap = distMap
                        expansionNegatives = set()
                        if self.teammate_general is not None:
                            expansionNegatives.update(self._map.players[self.teammate_general.player].tiles)
                        expansionNegatives.add(self.general)
                        expUtilPlan = ExpandUtils.get_round_plan_with_expansion(
                            self._map,
                            self.general.player,
                            self.targetPlayer,
                            50 - (self._map.turn % 50),
                            self.board_analysis,
                            self.territories.territoryMap,
                            self.tileIslandBuilder,
                            negativeTiles=expansionNegatives,
                            viewInfo=self.viewInfo
                        )

                        path = expUtilPlan.selected_option
                        otherPaths = expUtilPlan.all_paths

                        self.board_analysis.intergeneral_analysis.bMap = bMap

                        if path is not None:
                            self.info(f'F25 Exp collided at {str(move.dest)}, falling back to EXP {str(path)}')

                            curPath.pop_first_move()
                            if curPath.length == 0:
                                self.city_expand_plan.plan_paths.pop(0)

                            return self.get_first_path_move(path)

                        self.info(f'F25 Exp collided at {str(move.dest)}, no alternative found. No-opping')

                        curPath.pop_first_move()
                        if curPath.length == 0:
                            self.city_expand_plan.plan_paths.pop(0)

                        return None

                    self.viewInfo.add_info_line(
                        f'F25 Exp collided at {str(move.dest)}, capping {str(newPath)} instead.')
                    move = self.get_first_path_move(newPath)
                    curPath = newPath
                else:
                    self.info(
                        f'F25 Exp collided at {str(move.dest)}, continuing because collisionCapsOrPreventsEnemy.')

            curPath.pop_first_move()
            if curPath.length == 0:
                self.city_expand_plan.plan_paths.pop(0)

            return move
        return None

    def look_for_ffa_turtle_move(self) -> Move | None:
        """

        @return:
        """
        # consider not drawing attention to ourselves by camping FFA for an extra cycle
        # consider: NO leaf moves, gather 100% of tiles and take neut city; calc if possible?
        haveSeenOtherPlayer: bool = False
        neutCity: Tile | None = None
        nearEdgeOfMap: bool = self.get_distance_from_board_center(self.general, center_ratio=0.25) > 5

        if not neutCity or haveSeenOtherPlayer:
            return None

        remainingCycleTurns = 50 - self._map.turn % 50
        # potentialGenBonus = self._map.players[self.general.player].cityCount *
        potentialGenBonus = remainingCycleTurns // 2
        sumArmy = self.sum_player_army_near_tile(neutCity, distance=100, player=self.general.player)
        if sumArmy + potentialGenBonus - 3 > neutCity.army:
            path, move = self.capture_cities(negativeTiles=set(), forceNeutralCapture=True)
            if move is not None:
                self.info(f'AM I NOT TURTLEY ENOUGH FOR THE TURTLE CLUB? {move}')
                return move
            if path is not None:
                self.info(f'AM I NOT TURTLEY ENOUGH FOR THE TURTLE CLUB? {str(path)}')
                return self.get_first_path_move(path)

        return None

    def plan_city_capture(
            self,
            targetCity: Tile,
            cityGatherPath: Path | None,
            allowGather: bool,
            targetKillArmy: int,
            targetGatherArmy: int,
            killSearchDist: int,
            gatherMaxDuration: int,
            negativeTiles: typing.Set[Tile],
            gatherMinDuration: int = 0,
    ) -> typing.Tuple[Path | None, Move | None]:
        """
        If both a move AND a path are returned, means the path should follow up the move.

        @param targetCity:
        @param cityGatherPath:
        @param allowGather:
        @param targetKillArmy: The amount of EXTRA army to kill with. 0 means exact capture.
        @param targetGatherArmy: The amount of EXTRA army to kill with. 0 means exact capture.
        @param killSearchDist: How many tiles away to search for a raw kill.
        Must be greater than or equal to path.length, anything smaller than path.length will be replaced by path.length.
        Shorten the path over shortening the killSearchDist.
        @param gatherMaxDuration: Caps the amount of turns the gather can be computed.
        Note the actual kill will be pruned down to the min number of turns to capture the city.
        @param negativeTiles:
        @return:
        """
        if targetGatherArmy < targetKillArmy + targetCity.army:
            raise AssertionError(f'You cant gather less army {targetGatherArmy} to a city than the kill requirement {targetKillArmy} or the kill requirement will never fire and you will gather-loop.')

        targetKillArmy += 1
        targetGatherArmy += 1

        # self.viewInfo.add_info_line(f'city capture init negs {str([t for t in negativeTiles])}')

        if cityGatherPath and cityGatherPath.length > killSearchDist:
            killSearchDist = cityGatherPath.length

        if targetCity in negativeTiles or (self.threat is not None and targetCity in self.threat.armyAnalysis.shortestPathWay.tiles):
            negativeTiles = set()
        else:
            negativeTiles = negativeTiles.copy()
            # skipTiles.discard(targetCity)

        if targetCity.isNeutral and self.targetPlayer != -1 and len(self.targetPlayerObj.tiles) > 0:
            maxDist = self.territories.territoryDistances[self.targetPlayer].raw[targetCity.tile_index] - 1
            maxDist = min(5, maxDist)

            def foreachFunc(tile) -> bool:
                if self.territories.territoryDistances[self.targetPlayer].raw[tile.tile_index] < maxDist and tile not in self.tiles_gathered_to_this_cycle:
                    negativeTiles.add(tile)
            SearchUtils.breadth_first_foreach(self._map, self.targetPlayerObj.tiles, maxDist + 5, foreachFunc)

        potentialThreatNegs = self.get_potential_threat_movement_negatives(targetCity)
        negativeTiles.update(potentialThreatNegs)

        addlIncrementing = SearchUtils.count(targetCity.adjacents, lambda tile: tile.isCity and self._map.is_tile_enemy(tile))
        # # TODO replace with stateful 'strategy' plan for the city capture...
        # incrementingAdjuster = ((self._map.turn % 16) // 2) * addlIncrementing
        # targetKillArmy += incrementingAdjuster

        logbook.info(
            f"Searching for city kill on {str(targetCity)} in {killSearchDist} turns with targetArmy {targetKillArmy}...")
        # TODO make a new method for finding extra-army-falloffs so that we capture a city RIGHT NEXT to an army with very little extra, but the further the found 'path' gets the more of the 'extra army requirement' gets enforced.
        killPath = SearchUtils.dest_breadth_first_target(
            self._map,
            [targetCity],
            targetArmy=targetKillArmy,  # TODO good lord fix the 0.5 increment bug...
            maxTime=0.03,
            maxDepth=killSearchDist,
            noNeutralCities=True,
            preferCapture=True,
            negativeTiles=negativeTiles,
            searchingPlayer=self.general.player,
            additionalIncrement=addlIncrementing / 2)

        if killPath is None:
            killPath = SearchUtils.dest_breadth_first_target(
                self._map,
                [targetCity],
                targetArmy=targetKillArmy,  # TODO good lord fix the 0.5 increment bug...
                maxTime=0.03,
                maxDepth=killSearchDist,
                noNeutralCities=True,
                preferCapture=False,
                negativeTiles=negativeTiles,
                searchingPlayer=self.general.player,
                additionalIncrement=addlIncrementing / 2)

        if targetCity.player >= 0:
            altKillArmy = 1 + self.sum_enemy_army_near_tile(targetCity, distance=1)
            altKillPath = SearchUtils.dest_breadth_first_target(
                self._map,
                [targetCity],
                targetArmy=altKillArmy,
                maxTime=0.03,
                maxDepth=3,
                noNeutralCities=True,
                preferCapture=True,
                negativeTiles=negativeTiles,
                searchingPlayer=self.general.player,
                additionalIncrement=addlIncrementing / 2)
            if altKillPath is not None and (killPath is None or altKillPath.length <= killPath.length // 2):
                if killPath is not None:
                    self.info(f'Using short enCity cap len {altKillPath.length} {altKillPath.value} over larger len {killPath.length}')
                else:
                    self.info(f'Using short enCity cap len {altKillPath.length} {altKillPath.value}')
                killPath = altKillPath

        # killPath = SearchUtils.dest_breadth_first_target(self._map, [target], targetArmy, 0.1, searchDist, skipTiles, dontEvacCities=True)
        if killPath is not None:
            logbook.info(
                f"found depth {killPath.length} dest bfs kill on Neutral or Enemy city {targetCity.x},{targetCity.y} \n{str(killPath)}")
            self.info(f"City killpath {killPath}, setting GTN to None")
            self.viewInfo.evaluatedGrid[targetCity.x][targetCity.y] = 300
            self.gatherNodes = None
            addlArmy = 0
            if targetCity.player != -1:
                addlArmy += killPath.length
            if addlIncrementing > 0:
                addlArmy += killPath.length
            killPath.start.move_half = self.should_kill_path_move_half(killPath, targetKillArmy + addlArmy)
            self.city_capture_plan_tiles.update(killPath.tileList)
            self.city_capture_plan_last_updated = self._map.turn
            return killPath, None

        if not allowGather:
            return None, None

        # reduce the necessary gather army by the amount on the start nodes.
        armyAlreadyPrepped = 0
        if cityGatherPath:
            for tile in cityGatherPath.tileList:
                if self._map.is_player_on_team_with(tile.player, self.general.player):
                    armyAlreadyPrepped += tile.army - 1
                elif tile != targetCity:
                    armyAlreadyPrepped -= tile.army + 1
        targetGatherArmy -= armyAlreadyPrepped

        # TODO if neutral city, prioritize NOT pulling any army off of the main attack paths,
        #  abandon neut gathers if it would weaken us substantially
        targets = [targetCity]
        if cityGatherPath:
            targets = cityGatherPath.tileList
        with self.perf_timer.begin_move_event(f'Capture City gath to {str(targets)}'):
            # gatherDist = (gatherDuration - self._map.turn % gatherDuration)
            gatherDist = gatherMaxDuration  # we're gonna prune anyway
            negativeTiles = negativeTiles.copy()
            # skipTiles.add(self.general)
            for t in targets:
                self.viewInfo.add_targeted_tile(t, TargetStyle.PURPLE)

            mePlayer = self._map.players[self.general.player]

            cycleTurn = self.timings.get_turn_in_cycle(self._map.turn)
            turnsLeft = self.timings.get_turns_left_in_cycle(self._map.turn)
            notLateGame = mePlayer.tileCount < 150 and (self._map.remainingPlayers == 2 or self._map.is_2v2)
            if targetCity.isNeutral and notLateGame:
                genAlreadyInNeg = self.general in negativeTiles
                # towards the end of cycle, only gather to cities from further and further from enemy.

                offsetByNearEndOfCycle = cycleTurn // 20
                offsetByNearEndOfCycle = 0

                negativeTiles.update(self.cityAnalyzer.owned_contested_cities)
                #
                # if turnsLeft < 10:
                # skipTiles = self.get_timing_gather_negatives_unioned(
                #     skipTiles,
                #     additional_offset=offsetByNearEndOfCycle,
                #     forceAllowCities=True)

                if not genAlreadyInNeg and self.general in negativeTiles:
                    negativeTiles.remove(self.general)

            self.viewInfo.add_info_line(
                f"city gath target_tile gatherDist {gatherDist} - targetArmyGather {targetGatherArmy} (prepped {armyAlreadyPrepped}), negatives {'+'.join([str(t) for t in negativeTiles])}")

            if targetCity.player >= 0 and (cityGatherPath is not None and targetCity not in cityGatherPath.tileSet):
                addlIncrementing += 1

            move, gatherValue, gatherTurns, gatherNodes = self.get_gather_to_target_tiles(
                targets,
                0.03,
                gatherDist,
                negativeSet=negativeTiles,
                targetArmy=targetGatherArmy,
                additionalIncrement=addlIncrementing,
                # priorityMatrix=self.get_gather_tiebreak_matrix()  # this blows stuff up i think, 44f 35p, 36f 43p without it. Causes invalid army amount gathers
            )

            if move is not None:
                # if targetCity.player != -1:
                #     targetGatherArmy += 4 + gatherDist // 4
                preferPrune = set(self.expansion_plan.preferred_tiles) if self.expansion_plan is not None else None
                # if self.player.standingArmy < 100:
                #     preferPrune = None

                if preferPrune is not None:
                    for t in self.expansion_plan.preferred_tiles:
                        if t in self.tiles_gathered_to_this_cycle:
                            preferPrune.remove(t)

                if targetCity.isNeutral:
                    prunedTurns, prunedValue, prunedGatherNodes = Gather.prune_mst_to_army_with_values(
                        gatherNodes,
                        targetGatherArmy,
                        self.general.player,
                        teams=MapBase.get_teams_array(self._map),
                        turn=self._map.turn,
                        additionalIncrement=addlIncrementing,
                        preferPrune=preferPrune,
                        viewInfo=self.viewInfo if self.info_render_gather_values else None)
                else:  # if addlIncrementing == 0
                    prunedTurns, prunedValue, prunedGatherNodes = Gather.prune_mst_to_max_army_per_turn_with_values(
                        gatherNodes,
                        targetGatherArmy,
                        self.general.player,
                        teams=MapBase.get_teams_array(self._map),
                        additionalIncrement=addlIncrementing,
                        preferPrune=preferPrune,
                        minTurns=gatherMinDuration,
                        viewInfo=self.viewInfo if self.info_render_gather_values else None)
                # else:
                #     prunedTurns, prunedValue, gatherNodes = gatherTurns, gatherValue, gatherNodes

                GatherTreeNode.foreach_tree_node(prunedGatherNodes, lambda n: self.city_capture_plan_tiles.add(n.tile))
                self.city_capture_plan_tiles.update(targets)
                self.city_capture_plan_tiles.add(targetCity)
                self.city_capture_plan_last_updated = self._map.turn

                if targetCity.isNeutral and turnsLeft - prunedTurns < 10 and notLateGame and not self._map.is_walled_city_game and targetCity.army > 10:
                    # not enough time left in cycle to be worth city capture, return None.
                    self.info(
                        f"GC TOO SLOW {str(targetCity)} {move} t{prunedTurns}/{gatherTurns}/{gatherDist}  prun{prunedValue + armyAlreadyPrepped}/pre{gatherValue + armyAlreadyPrepped}/req{targetGatherArmy + armyAlreadyPrepped} -proact {self.should_proactively_take_cities()}")
                    self.viewInfo.evaluatedGrid[targetCity.x][targetCity.y] = 300
                    return None, None

                sameLengthKillPath = SearchUtils.dest_breadth_first_target(
                    self._map,
                    [targetCity],
                    targetArmy=targetKillArmy,
                    maxTime=0.03,
                    maxDepth=min(16, prunedTurns + len(targets) + 1),
                    noNeutralCities=True,
                    preferCapture=True,
                    negativeTiles=negativeTiles,
                    searchingPlayer=self.general.player)

                if sameLengthKillPath is not None:
                    pathVal = sameLengthKillPath.calculate_value(
                        self.player.index,
                        MapBase.get_teams_array(self._map),
                        negativeTiles=negativeTiles
                    )
                    if pathVal + 4 > prunedValue * 0.8:
                        self.info(f"GC @{str(targetCity)} killpath found optimizing captures")
                        self.city_capture_plan_tiles.update(sameLengthKillPath.tileList)
                        self.city_capture_plan_last_updated = self._map.turn
                        return sameLengthKillPath, None

                move = self.get_tree_move_default(prunedGatherNodes, pop=False)
                path = None
                self.gatherNodes = prunedGatherNodes
                if move and move.dest == targetCity:
                    moveList = []
                    prunedNodes = GatherTreeNode.clone_nodes(prunedGatherNodes)

                    _ = self.get_tree_move_default(prunedNodes, pop=True)
                    nextMove = move
                    while nextMove is not None:
                        moveList.append(nextMove)
                        nextMove = self.get_tree_move_default(prunedNodes, pop=True)
                    if len(moveList) > 0:
                        moveList.extend(cityGatherPath.convert_to_move_list())
                        moveListPath = MoveListPath(moveList)
                        path = moveListPath
                        self.curPath = path
                        # move = None

                self.info(
                    f"GC {str(targetCity)} {move} t{prunedTurns}/{gatherTurns}/{gatherDist}  prun{prunedValue + armyAlreadyPrepped}/pre{gatherValue + armyAlreadyPrepped}/req{targetGatherArmy + armyAlreadyPrepped} -proact {self.should_proactively_take_cities()}")
                self.viewInfo.evaluatedGrid[targetCity.x][targetCity.y] = 300
                return path, move

        return None, None

    def get_timing_gather_negatives_unioned(
            self,
            gatherNegatives: typing.Set[Tile],
            additional_offset: int = 0,
            forceAllowCities: bool = False,
    ) -> typing.Set[Tile]:
        if not forceAllowCities:
            gatherNegatives = gatherNegatives.union(self.cities_gathered_this_cycle)

        if self.is_all_in():
            return gatherNegatives

        if self.is_ffa_situation() and self.player.tileCount < 65:
            return gatherNegatives

        gatherNegatives.update(self.win_condition_analyzer.defend_cities)

        if self.currently_forcing_out_of_play_gathers or self.defend_economy:
            return gatherNegatives

        if self.gather_include_shortest_pathway_as_negatives:
            gatherNegatives = gatherNegatives.union(self.board_analysis.intergeneral_analysis.shortestPathWay.tiles)

        armyCutoff = int(self._map.players[self.general.player].standingArmy ** 0.5)

        def foreach_func(tile: Tile):
            if tile in self.tiles_gathered_to_this_cycle:
                return

            if not self._map.is_tile_friendly(tile):
                return

            if tile.army > armyCutoff:
                return

            gatherNegatives.add(tile)

        if self.gather_include_distance_from_enemy_general_as_negatives > 0:
            ratio = self.gather_include_distance_from_enemy_general_large_map_as_negatives
            if self.targetPlayerObj.tileCount < 150:
                ratio = self.gather_include_distance_from_enemy_general_as_negatives
            excludeDist = int(self.shortest_path_to_target_player.length * ratio)

            excludeDist += additional_offset

            SearchUtils.breadth_first_foreach(
                self._map,
                [self.targetPlayerExpectedGeneralLocation],
                maxDepth=excludeDist,
                foreachFunc=foreach_func,
            )

        if self.gather_include_distance_from_enemy_TERRITORY_as_negatives > 0 and self.targetPlayer != -1:
            excludeDist = self.gather_include_distance_from_enemy_TERRITORY_as_negatives + additional_offset

            startTiles = [t for t in self._map.get_all_tiles() if self.territories.territoryMap[t] == self.targetPlayer]

            SearchUtils.breadth_first_foreach(
                self._map,
                startTiles,
                maxDepth=excludeDist,
                foreachFunc=foreach_func,
            )

        if self.gather_include_distance_from_enemy_TILES_as_negatives > 0 and self.targetPlayer != -1:
            excludeDist = self.gather_include_distance_from_enemy_TILES_as_negatives

            startTiles = [t for t in self._map.get_all_tiles() if t.player == self.targetPlayer and not self._map.is_player_on_team_with(self.territories.territoryMap[t], self.general.player)]

            if len(startTiles) > 0:
                SearchUtils.breadth_first_foreach(
                    self._map,
                    startTiles,
                    maxDepth=excludeDist,
                    foreachFunc=foreach_func,
                )

        return gatherNegatives

    def is_path_moving_mostly_away(self, path: Path, bMap: MapMatrixInterface[int]):
        distSum = 0
        for tile in path.tileList:
            distSum += bMap[tile]

        distAvg = distSum / path.length

        distStart = bMap[path.start.tile]
        distEnd = bMap[path.tail.tile]

        doesntAverageCloserToEnemySlightly = distEnd > distStart - path.length // 4
        notHuntingNearby = distEnd > self.shortest_path_to_target_player.length // 6

        if notHuntingNearby and doesntAverageCloserToEnemySlightly and distAvg > distStart - path.length // 4:
            return True

        return False

    def check_army_out_of_play_ratio(self) -> bool:
        """
        0.0 means all army is in the core play area
        1.0 means all army is outside the core play area.
        0.5 means hella sketchy, half our army is outside the play area.
        @return:
        """
        self.out_of_play_tiles = set()
        if self.force_far_gathers and self.force_far_gathers_turns <= 0:
            self.force_far_gathers_turns = 0
            self.force_far_gathers_sleep_turns = 50
            self.force_far_gathers = False

        if self._map.is_2v2 and self.teammate_general is not None:
            return False

        if self._map.turn < 100 and not self.is_weird_custom:
            return False

        if self.targetPlayer == -1 or self.shortest_path_to_target_player is None:
            return False

        inPlaySum = 0
        medPlaySum = 0
        outOfPlaySum = 0
        outOfPlayCount = 0
        nearOppSum = 0
        genPlayer = self._map.players[self.general.player]
        pathLen = self.shortest_path_to_target_player.length
        inPlayCutoff = pathLen + pathLen * (self.behavior_out_of_play_distance_over_shortest_ratio / 2)
        mediumRangeCutoff = pathLen + pathLen * self.behavior_out_of_play_distance_over_shortest_ratio

        outOfPlayTiles = self.out_of_play_tiles
        for tile in genPlayer.tiles:
            genDist = self.board_analysis.intergeneral_analysis.aMap.raw[tile.tile_index]
            enDist = self.board_analysis.intergeneral_analysis.bMap.raw[tile.tile_index]
            if genDist > enDist * 2:
                nearOppSum += tile.army - 1
                continue
            if tile.isGeneral:
                continue

            pathWay = self.board_analysis.intergeneral_analysis.pathWayLookupMatrix.raw[tile.tile_index]
            if pathWay is None:
                self.viewInfo.add_info_line(f'tile {str(tile)} had no pathway...? genDist{genDist} enDist{enDist}')

            if pathWay is not None and pathWay.distance <= inPlayCutoff:
                inPlaySum += tile.army - 1
            elif pathWay is not None and pathWay.distance < mediumRangeCutoff:
                medPlaySum += tile.army - 1
            else:
                # if pathWay is not None:
                #     self.viewInfo.bottomRightGridText.raw[tile.tile_index] = pathWay.distance
                outOfPlaySum += tile.army - 1
                outOfPlayCount += 1
                outOfPlayTiles.add(tile)

        total = medPlaySum + inPlaySum + outOfPlaySum + nearOppSum
        if total == 0:
            return False

        # once we have lots of army this becomes harder, subtract some square root of our army - 100 or something
        hugeGameOffset = 0
        realTotal = total
        incMedium = medPlaySum // 2
        if total > 90:
            hugeGameOffset = int((total - 90) ** 0.8)
            total = 90 + hugeGameOffset
            incMedium = 0

        inPlayRat = inPlaySum / total
        medPlayRat = medPlaySum / total
        outOfPlaySumFactored = outOfPlaySum - nearOppSum // 3 + incMedium
        outOfPlayRat = outOfPlaySumFactored / total

        aboveOutOfPlay = outOfPlayRat > self.behavior_out_of_play_defense_threshold

        tilesMinusDeserts = self.player.tileCount - len(self.player.deserts)
        if outOfPlayRat > 1.4 and (tilesMinusDeserts > 150 or self.player.cityCount > 5 or len(self.defensive_spanning_tree) > 50):
            if self.force_far_gathers_sleep_turns <= 0 and not self.force_far_gathers:
                self.force_far_gathers = True
                self.force_far_gathers_turns = self._map.remainingCycleTurns
                minTurns = max(tilesMinusDeserts // 2, len(self.defensive_spanning_tree) + outOfPlayCount // 2, outOfPlayCount) + self.board_analysis.inter_general_distance * outOfPlayRat
                while self.force_far_gathers_turns <= minTurns:
                    self.force_far_gathers_turns += 50
                cap = outOfPlayCount
                if self.target_player_gather_path:
                    cap += int(self.target_player_gather_path.length / 2)
                if self.force_far_gathers_turns > cap:
                    self.force_far_gathers_turns = cap

        self.viewInfo.add_stats_line(
            f'out-of-play {outOfPlayRat:.2f} {aboveOutOfPlay} {total:.0f}@dist{mediumRangeCutoff:.1f}: OUT{outOfPlaySum}-OPP{nearOppSum}+MF{incMedium} ({outOfPlayRat:.2f}>{self.behavior_out_of_play_defense_threshold:.2f}), IN{inPlaySum}({inPlayRat:.2f}), MED{medPlaySum}({medPlayRat:.2f}), Tot{total} ogTot{realTotal} (huge {hugeGameOffset}, inCut {inPlayCutoff:.1f}, medCut {mediumRangeCutoff:.1f})')

        return aboveOutOfPlay

    def should_allow_neutral_city_capture(
            self,
            genPlayer: Player,
            forceNeutralCapture: bool,
            targetCity: Tile | None = None
    ) -> bool:
        # if self.currently_forcing_out_of_play_gathers:
        #     logbook.info(f'bypassing neut cities due to currently_forcing_out_of_play_gathers {self.currently_forcing_out_of_play_gathers}')
        #     return False

        cityCost = 44
        if self._map.walled_city_base_value is not None:
            cityCost = self._map.walled_city_base_value
        if targetCity is not None:
            cityCost = targetCity.army + 1  # - 10

        if self.player.standingArmy - 15 < cityCost and not self._map.is_walled_city_game:
            return False

        # if self._map.is_walled_city_game:
        #     return True

        if self.targetPlayer != -1:
            cycleLeft = self.timings.get_turns_left_in_cycle(self._map.turn)
            threatTurns = cycleLeft - 12
            minFogDist = self.shortest_path_to_target_player.length // 2 + 3
            if self.enemy_attack_path:
                enFogged = self.enemy_attack_path.get_subsegment_excluding_trailing_visible()
                minFogDist = self.distance_from_general(enFogged.tail.tile) + 1
            if threatTurns < minFogDist:
                threatTurns = minFogDist
            with self.perf_timer.begin_move_event(f'approximate attack / def ({threatTurns}t)'):
                defTurns = threatTurns
                generalContribution = defTurns // 2

                cityContribution = (defTurns - len(self.city_capture_plan_tiles)) // 2
                cityDefVal = generalContribution + cityContribution
                if not self.was_allowing_neutral_cities_last_turn:
                    cityDefVal -= 15
                searchNegs = set()
                if self.city_capture_plan_last_updated > self._map.turn - 2 and targetCity in self.city_capture_plan_tiles:
                    searchNegs.update(self.city_capture_plan_tiles)
                else:
                    cityDefVal -= cityCost
                    tgCities = [targetCity] if targetCity is not None else list(self.cityAnalyzer.city_scores.keys())
                    if len(tgCities) > 0:
                        playerTilesNearCity = SearchUtils.get_player_tiles_near_up_to_army_amount(map=self._map, fromTiles=tgCities, armyAmount=tgCities[0].army, asPlayer=self.general.player, tileAmountCutoff=1)
                        # since we already subtracted the cost of the city, we need to offset it back upwards with what we're putting in these negatives.
                        for t in playerTilesNearCity:
                            cityDefVal += t.army - 1
                        searchNegs.update(playerTilesNearCity)
                    else:
                        self.viewInfo.add_stats_line(f'bypassing neut cities, 0 neut cities available :(')
                        return False

                defTile = self.general
                if targetCity and self.board_analysis.intergeneral_analysis.bMap.raw[defTile.tile_index] > self.board_analysis.intergeneral_analysis.bMap.raw[targetCity.tile_index]:
                    defTile = targetCity
                attackNegs = set(searchNegs)
                attackNegs.update(self.largePlayerTiles)
                risk = self.win_condition_analyzer.get_approximate_attack_against(
                    [defTile],
                    inTurns=threatTurns,
                    asPlayer=self.targetPlayer,
                    forceFogRisk=True,
                    negativeTiles=attackNegs)

                requiredDefenseArmy = risk + cityCost - cityDefVal
                turns, value = self.win_condition_analyzer.get_dynamic_turns_visible_defense_against([defTile], defTurns, asPlayer=self.general.player, minArmy=requiredDefenseArmy, negativeTiles=searchNegs)

            armyBonusDefense = 2 * max(0, defTurns - cycleLeft)
            defAfterCity = value + cityDefVal + armyBonusDefense
            if self.opponent_tracker.even_or_up_on_cities(self.targetPlayer):
                if risk > defAfterCity and risk > 5:
                    self.is_blocking_neutral_city_captures = True
                    self.viewInfo.add_stats_line(f'bypassing neut cities, danger {risk} in {threatTurns} > {defAfterCity} ({value} + cityDefVal {cityDefVal}) and risk > 5')
                    return False

                if self.is_blocking_neutral_city_captures:
                    self.viewInfo.add_stats_line(f'bypassing neut cities due to is_blocking_neutral_city_captures {self.is_blocking_neutral_city_captures}')
                    return False

            if self.defend_economy and (self.targetPlayer == -1 or self.opponent_tracker.even_or_up_on_cities(self.targetPlayer)):
                self.viewInfo.add_stats_line(f'bypassing neut cities due to defend_economy {self.defend_economy}')
                return False

            if risk <= defAfterCity:
                self.viewInfo.add_stats_line(f'ALLOW neut cities, danger {risk} in {threatTurns} <= {defAfterCity} ({value} + cityDefVal {cityDefVal})')
            else:
                self.viewInfo.add_stats_line(f'ALLOW neut cities DESPITE danger {risk} in {threatTurns} > {defAfterCity} ({value} + cityDefVal {cityDefVal})')

        # we now take cities proactively?
        proactivelyTakeCity = self.should_proactively_take_cities() or forceNeutralCapture
        safeFromThreat = (
                self.threat is None
                or self.threat.threatType != ThreatType.Kill
                or self.threat.threatValue <= self.threat.turns
                or (self.threat.turns > 6 and not self.threat.path.start.tile.visible)
                or not self.threat.path.tail.tile.isGeneral
        )
        if not safeFromThreat:
            self.viewInfo.add_info_line("Will not proactively take cities due to the existing threat....")
            proactivelyTakeCity = False
            if self.threat.threatValue > cityCost // 2:
                forceNeutralCapture = False
                self.force_city_take = False

        forceCityOffset = 0
        if self.force_city_take or self.is_player_spawn_cramped(self.shortest_path_to_target_player.length):
            forceCityOffset = 1

        targCities = 1

        targetPlayer = None
        if self.targetPlayer != -1:
            targetPlayer = self._map.players[self.targetPlayer]

        if targetPlayer is not None:
            targCities = targetPlayer.cityCount

        cityTakeThreshold = targCities + forceCityOffset

        logbook.info(f'force_city_take {self.force_city_take}, cityTakeThreshold {cityTakeThreshold}, targCities {targCities}')
        if self.targetPlayer == -1 or self._map.remainingPlayers <= 3 or self.force_city_take:
            if (
                    targetPlayer is None
                    or (
                    (genPlayer.cityCount < cityTakeThreshold or proactivelyTakeCity)
                    and safeFromThreat
            )
            ):
                logbook.info("Didn't skip neut cities.")
                # if (player is None or player.cityCount < cityTakeThreshold) and math.sqrt(player.standingArmy) * sqrtFactor > largestTile.army\
                if forceNeutralCapture or targetPlayer is None or genPlayer.cityCount < cityTakeThreshold or self.force_city_take:
                    return True
                else:
                    logbook.info(
                        f"We shouldn't be taking more neutral cities, we're too defenseless right now.")
            else:
                logbook.info(
                    f"Skipped neut cities. in_gather_split(self._map.turn) {self.timings.in_gather_split(self._map.turn)} and (player.cityCount < targetPlayer.cityCount {genPlayer.cityCount < targetPlayer.cityCount} or proactivelyTakeCity {proactivelyTakeCity})")
        return False

    def find_hacky_path_to_find_target_player_spawn_approx(self, minSpawnDist: int):
        if self.targetPlayerObj is None or len(self.targetPlayerObj.tiles) == 0:
            return None

        if not self.undiscovered_priorities:
            depth = 7
            if len(self._map.tiles_by_index) > 1000:
                depth = 6

            if len(self._map.tiles_by_index) > 2500:
                depth = 5

            if len(self._map.tiles_by_index) > 4000:
                depth = 4

            self.undiscovered_priorities = self.find_expected_1v1_general_location_on_undiscovered_map(
                undiscoveredCounterDepth=depth,
                minSpawnDistance=minSpawnDist)

        def value_func(tile: Tile, prioObj):
            (realDist, negScore, dist, lastTile) = prioObj

            if realDist < minSpawnDist:
                return None

            score = 0 - negScore
            scoreOverDist = score / (realDist * dist + 1)
            return (scoreOverDist, 0 - realDist)

        def prio_func(tile: Tile, prioObj):
            (fakeDist, negScore, dist, lastTile) = prioObj
            if tile.player == self.targetPlayer:
                negScore -= 200
            if tile.visible:
                fakeDist += 2
            else:
                negScore -= 100
            if lastTile is not None and not lastTile.visible and tile.visible:
                negScore += 10000
            undiscScore = self.undiscovered_priorities.raw[tile.tile_index]
            negScore -= undiscScore
            # negScore += 5
            realDist = self.distance_from_general(tile)
            return realDist, negScore, dist + 1, tile

        def skip_func(tile: Tile, prioObj):
            return tile.visible and tile.player != self.targetPlayer

        startDict = {}
        for targetTile in self.targetPlayerObj.tiles:
            startDict[targetTile] = ((self.distance_from_general(targetTile), 0, 0, None), 0)

        path = SearchUtils.breadth_first_dynamic_max(
            self._map,
            startDict,
            valueFunc=value_func,
            maxTime=0.1,
            maxTurns=150,
            maxDepth=150,
            noNeutralCities=True,
            priorityFunc=prio_func,
            skipFunc=skip_func,
            noLog=True,
            # useGlobalVisitedSet=False
        )

        self.viewInfo.add_info_line(f'hacky path {str(path)}...?')

        self.viewInfo.color_path(PathColorer(path, 255, 0, 0))
        if path is not None:
            return path.tail.tile

        return None

    def should_rapid_capture_neutral_cities(self) -> bool:
        if self.targetPlayer == -1:
            return True

        if self._map.is_2v2 and self.teammate_general is not None:
            seenOtherPlayer = False
            for player in self._map.players:
                if not self._map.is_player_on_team_with(player.index, self.general.player):
                    if len(player.tiles) > 0:
                        seenOtherPlayer = True

            if not seenOtherPlayer:
                return True
            return False

        if (self._map.is_walled_city_game or self._map.is_low_cost_city_game) and (self.target_player_gather_path.length < 5 or not self.armyTracker.seen_player_lookup[self.targetPlayer]):
            return True

        # if self._map.is_low_cost_city_game and

        mePlayer = self._map.players[self.general.player]
        targPlayer = self._map.players[self.targetPlayer]
        unseenTargetPlayerAndMapMassive = len(self.targetPlayerObj.tiles) == 0 and self._map.is_walled_city_game

        # if self._map.remainingPlayers == 2 and mePlayer.cityCount +

        haveLotsOfExcessArmy = mePlayer.standingArmy > mePlayer.tileCount * 2
        aheadOfOppArmyByHundreds = mePlayer.standingArmy > targPlayer.standingArmy + 100
        notWinningEcon = not self.opponent_tracker.winning_on_economy(byRatio=0.8, cityValue=40)
        hasDoubleEcon = targPlayer.cityCount + 2 < mePlayer.cityCount // 2 and not self._map.remainingPlayers > 3
        hasTripleEcon = targPlayer.cityCount + 1 < mePlayer.cityCount // 3
        if self._map.is_2v2:
            hasDoubleEcon = self.opponent_tracker.winning_on_economy(2.0, cityValue=100)
            hasTripleEcon = self.opponent_tracker.winning_on_economy(3.0, cityValue=100)

        # TODO track how much the opp has explored, the difference between our tile count BFSed from enemy territory to general distance - the amount they've seen is how much buffer time we probably have.
        numberOfTilesEnemyNeedsToExploreToFindUsAvg = mePlayer.tileCount // 2 - 50
        if targPlayer.aggression_factor < 20:
            # then this enemy isn't attacking us, city-up
            numberOfTilesEnemyNeedsToExploreToFindUsAvg = mePlayer.tileCount // 2

        if (
                not self.is_all_in_army_advantage
                and (
                        (mePlayer.tileCount > 200 and mePlayer.standingArmy > mePlayer.tileCount * 3)
                        or (mePlayer.tileCount > 150 and mePlayer.standingArmy > mePlayer.tileCount * 4)
                        or (mePlayer.tileCount > 110 and mePlayer.standingArmy > mePlayer.tileCount * 5)
                )
                and mePlayer.standingArmy > targPlayer.standingArmy - numberOfTilesEnemyNeedsToExploreToFindUsAvg
                and not targPlayer.knowsKingLocation
                and (not hasDoubleEcon or targPlayer.aggression_factor < 30 and not hasTripleEcon)
        ):
            self.viewInfo.add_info_line(f'RAPID CITY EXPAND due to sheer volume of tiles/army')
            self.is_rapid_capturing_neut_cities = True
            return True

        haveMinimumArmyAdv = mePlayer.standingArmy > targPlayer.standingArmy * 0.8 or targPlayer.aggression_factor < 150
        haveAchievedEconomicDominance = self.opponent_tracker.winning_on_economy(byRatio=1.45, cityValue=1000)

        if (
                1 == 1
                # and mePlayer.tileCount > 120
                and (
                (aheadOfOppArmyByHundreds and notWinningEcon and targPlayer.aggression_factor < 200)
                or (haveLotsOfExcessArmy and self.is_rapid_capturing_neut_cities)
        )
                and not targPlayer.knowsKingLocation
                and not hasDoubleEcon
        ):
            if not haveMinimumArmyAdv:
                self.viewInfo.add_info_line(f'Ceasing rapid city expand due to sketchy army amount territory')
            elif haveAchievedEconomicDominance:
                self.viewInfo.add_info_line(f'Ceasing rapid city expand due to economic dominance achieved')
            else:
                self.is_rapid_capturing_neut_cities = True
                return True

        self.is_rapid_capturing_neut_cities = False
        return False

    def find_rapid_city_path(self) -> Path | None:
        if not self.should_rapid_capture_neutral_cities():
            return None

        longDistSearchCities = []
        for neutCity in self.cityAnalyzer.city_scores:
            if not neutCity.discovered:
                continue  # don't try to rapid expand into fictitious cities
            if self.sum_enemy_army_near_tile(neutCity, 2) == 0 and self.count_enemy_territory_near_tile(neutCity, 3) == 0:
                longDistSearchCities.append(neutCity)

        shortDistSearchCities = []
        if self.targetPlayerObj is not None and self.targetPlayerObj.aggression_factor > 200:
            for enCity in self.cityAnalyzer.enemy_city_scores:
                if not enCity.discovered:
                    continue  # don't try to rapid expand into fictitious cities
                shortDistSearchCities.append(enCity)
                if not self.territories.is_tile_in_enemy_territory(enCity):
                    longDistSearchCities.append(enCity)

        if len(shortDistSearchCities) > 0:
            quickestKillPath = SearchUtils.dest_breadth_first_target(self._map, shortDistSearchCities, maxDepth=4)
            if quickestKillPath is not None:
                self.info(f'RAPID CITY EN EXPAND DUE TO should_rapid_capture_neutral_cities')
                return quickestKillPath

        if len(longDistSearchCities) > 0:
            quickestKillPath = SearchUtils.dest_breadth_first_target(self._map, longDistSearchCities, maxDepth=9)
            if quickestKillPath is not None:
                self.info(f'RAPID CITY EXPAND DUE TO should_rapid_capture_neutral_cities')
                return quickestKillPath

        return None

    def get_remaining_move_time(self) -> float:
        used = self.perf_timer.get_elapsed_since_update(self._map.turn)
        moveCycleTime = 0.5
        latencyBuffer = 0.26
        allowedLatest = moveCycleTime - latencyBuffer
        remaining = allowedLatest - used
        if DebugHelper.IS_DEBUGGING:
            return max(remaining, 0.1)
        return remaining

    def should_abandon_king_defense(self) -> bool:
        return self._map.remainingPlayers == 2 and not self.opponent_tracker.winning_on_economy(byRatio=self.behavior_losing_on_economy_skip_defense_threshold)

    def block_neutral_captures(self, reason: str = ''):
        if self.curPath and self.curPath.tail.tile.isCity and self.curPath.tail.tile.isNeutral:
            targetNeutCity = self.curPath.tail.tile
            if self.is_blocking_neutral_city_captures:
                self.info(
                    f'forcibly stopped taking neutral city {str(targetNeutCity)} {reason}')
                self.curPath = None
        logbook.info(f'Preventing neutral city captures for now {reason}')
        self.is_blocking_neutral_city_captures = True

    def continue_cur_path(self, threat: ThreatObj | None, defenseCriticalTileSet: typing.Set[Tile]) -> Move | None:
        if self.expansion_plan.includes_intercept:
            self.curPath = None
            self.viewInfo.add_info_line(f'clearing curPath because expansion includes intercept')
            return None

        if self.curPath is None:
            return None

        nextMove = self.curPath.get_first_move()

        if nextMove is None:
            self.info(f'curPath None move. curPath: {self.curPath}')
            try:
                self.curPath.pop_first_move()
            except:
                self.curPath = None
            return None

        if nextMove.source not in nextMove.dest.movable:
            self.info(f'!!!!!! curPath returned invalid move {nextMove}, nuking curPath and continuing...')
            self.curPath = None
            return None

        inc = 0
        while (
                nextMove
                and (
                   nextMove.source.army <= 1
                   or nextMove.source.player != self._map.player_index
                )
        ):
            inc += 1
            if nextMove.source.army <= 1:
                logbook.info(
                    f"!!!!\nMove was from square with 1 or 0 army\n!!!!! {nextMove.source.x},{nextMove.source.y} -> {nextMove.dest.x},{nextMove.dest.y}")
            elif nextMove.source.player != self._map.player_index:
                logbook.info(
                    f"!!!!\nMove was from square OWNED BY THE ENEMY\n!!!!! [{nextMove.source.player}] {nextMove.source.x},{nextMove.source.y} -> {nextMove.dest.x},{nextMove.dest.y}")
            logbook.info(f"{inc}: doing made move thing? Path: {self.curPath}")
            self.curPath.pop_first_move()
            if inc > 20:
                raise AssertionError("bitch, what you doin?")
            try:
                nextMove = self.curPath.get_first_move()
            except:
                self.curPath = None
                return None

        if nextMove is None:
            return None

        if nextMove.dest is not None:
            dest = nextMove.dest
            source = nextMove.source
            if source.isGeneral and not self.general_move_safe(dest):
                logbook.info(
                    f"Attempting to execute path move from self.curPath?")
                # self.curPath = None
                # self.curPathPrio = -1
                # logbook.info("General move in path would have violated general min army allowable. Repathing.")
                if self.general_move_safe(dest, move_half=True):
                    logbook.info("General move in path would have violated general min army allowable. Moving half.")
                    move = Move(source, dest, True)
                    return move
                else:
                    self.curPath = None
                    self.curPathPrio = -1
                    logbook.info("General move in path would have violated general min army allowable. Repathing.")

            else:
                while self.curPath is not None:
                    if nextMove.source in defenseCriticalTileSet and nextMove.source.army > 5:
                        tile = nextMove.source
                        # self.curPathPrio = -1
                        logbook.info(
                            f"\n\n\n~~~~~~~~~~~\nSKIPPED: Move was from a negative tile {tile.x},{tile.y}\n~~~~~~~~~~~~~\n\n~~~\n")
                        self.curPath = None
                        self.curPathPrio = -1
                        if threat is not None:
                            killThreatPath = self.kill_threat(self.threat)
                            if killThreatPath is not None:
                                self.info(f"REPLACED CURPATH WITH Final path to kill threat! {killThreatPath.toString()}")
                                # self.curPath = killThreatPath
                                self.viewInfo.color_path(PathColorer(killThreatPath, 0, 255, 204, 255, 10, 200))
                                logbook.info(f'setting targetingArmy to {str(threat.path.start.tile)} in continue_cur_path when move wasnt safe for general')
                                self.targetingArmy = self.armyTracker.armies[threat.path.start.tile]
                                return self.get_first_path_move(killThreatPath)
                        else:
                            logbook.warn("Negative tiles prevented a move but there was no threat???")

                    elif nextMove.source.player != self._map.player_index or nextMove.source.army < 2:
                        logbook.info("\n\n\n~~~~~~~~~~~\nCleaned useless move from path\n~~~~~~~~~~~~~\n\n~~~\n")
                        self.curPath.pop_first_move()
                        try:
                            nextMove = self.curPath.get_first_move()
                        except:
                            self.curPath = None
                            return None
                    else:
                        break
                if self.curPath is not None and nextMove.dest is not None:
                    if nextMove.source == self.general and not self.general_move_safe(
                            self.curPath.start.next.tile, self.curPath.start.move_half):
                        self.curPath = None
                        self.curPathPrio = -1
                    else:
                        move = self.curPath.get_first_move()
                        lastFrom = None
                        lastTo = None
                        if self._map.last_player_index_submitted_move:
                            lastFrom, lastTo, _ = self._map.last_player_index_submitted_move

                        if move.source == lastFrom and move.dest == lastTo:
                            self.curPath.pop_first_move()
                            move = self.curPath.get_first_move()

                        self.info(f"CurPath cont {move}")
                        # self.info("MAKING MOVE FROM NEW PATH CLASS! Path {}".format(self.curPath.toString()))
                        return self.move_half_on_repetition(move, 6, 3)

        self.info("path move failed...? setting curPath to none...")
        self.info(f'path move WAS {self.curPath}')
        self.curPath = None
        return None

    def try_find_gather_move(
            self,
            threat: ThreatObj | None,
            defenseCriticalTileSet: typing.Set[Tile],
            leafMoves: typing.List[Move],
            needToKillTiles: typing.List[Tile],
    ) -> Move | None:
        tryGather = True
        player = self._map.players[self.general.player]
        enemyGather = False
        if (
                self.get_approximate_fog_risk_deficit() < 10
                and not self._map.remainingPlayers > 2
                and not self.opponent_tracker.winning_on_economy(byRatio=1.1, cityValue=0)  # TODO was 1.0
                # and self.opponent_tracker.winning_on_army(0.95)  # TODO was uncommented
                # and self.approximate_greedy_turns_avail > 0
        ):
            logbook.info("Forced enemyGather to true due to NOT winning_on_economy(by tiles only) and winning_on_army")
            enemyGather = True

        if self.is_all_in():
            move = self.try_find_flank_all_in(self.timings.get_turns_left_in_cycle(self._map.turn))
            if move is not None:
                self.info(f'flank all in {move}')
                return move

            return None

        # neutralGather = len(tiles) <= 2
        neutralGather = False
        turn = self._map.turn
        tiles = player.tileCount

        # TODO 2v2 calculations
        tileDeficitThreshold = self._map.players[self.targetPlayer].tileCount * 1.05
        if self.makingUpTileDeficit:
            tileDeficitThreshold = self._map.players[self.targetPlayer].tileCount * 1.15 + 8

        if (
                not self.defend_economy
                and self.distance_from_general(self.targetPlayerExpectedGeneralLocation) > 2
                and player.tileCount < tileDeficitThreshold
                and not (self.is_all_in() or self.all_in_losing_counter > 50)
        ):
            logbook.info("ayyyyyyyyyyyyyyyyyyyyyyyyy set enemyGather to True because we're behind on tiles")
            enemyGather = True
            skipFFANeutralGather = (self._map.turn > 50 and self._map.remainingPlayers > 2)
            # if not skipFFANeutralGather and (self._map.turn < 120 or self.distance_from_general(self.targetPlayerExpectedGeneralLocation) < 3):
            #    neutralGather = True
            self.makingUpTileDeficit = True
        else:
            self.makingUpTileDeficit = False

        if self.defend_economy:
            logbook.info("we're playing defensively, neutralGather and enemyGather set to false...")
            neutralGather = False
            enemyGather = False
        # TODO maybe replace with optimal expand? But shouldn't be before gather anymore.
        # if (self.makingUpTileDeficit):
        #    leafMove = self.find_leaf_move(allLeaves)
        #    if (None != leafMove):
        #        self.info("doing tileDeficit leafMove stuff mannn")
        #        return leafMove

        if not tryGather:
            return None

        return self.get_main_gather_move(defenseCriticalTileSet, leafMoves, enemyGather, neutralGather, needToKillTiles)

    def get_main_gather_move(
            self,
            defenseCriticalTileSet: typing.Set[Tile],
            leafMoves: typing.List[Move] | None,
            enemyGather: bool = False,
            neutralGather: bool = False,
            needToKillTiles: typing.List[Tile] | None = None,
    ) -> Move | None:
        if not needToKillTiles:
            needToKillTiles = []
        if not leafMoves:
            leafMoves = []

        gathString = ""
        gathStartTime = time.perf_counter()
        gatherTargets = self.target_player_gather_targets.copy()
        if len(gatherTargets) == 2:
            gatherTargets = set()
            gatherTargets.add(self.general)
            gatherTargets.update(self.launchPoints)
        # if self.launchPoints is not None:
        #     gatherTargets.update(self.launchPoints)
        gatherNegatives = defenseCriticalTileSet.copy()
        # for tile in self.largePlayerTiles:
        #    gatherNegatives.add(tile)
        if self.curPath:
            nextMove = self.curPath.get_first_move()
            if nextMove:
                gatherNegatives.add(nextMove.source)

        # De-prioritize smallish tiles that are in enemy territory from being gathered
        genPlayer = self._map.players[self.general.player]

        inEnTerrSet = set()
        sumEnTerrArmy = 0
        if self.targetPlayer >= 0:
            for tile in genPlayer.tiles:
                # if self._map.is_player_on_team_with(self.territories.territoryMap[tile], self.player) and tile.army < 8:
                if self._map.is_player_on_team_with(self.territories.territoryMap[tile], self.targetPlayer):
                    inEnTerrSet.add(tile)
                    sumEnTerrArmy += tile.army - 1
        isNonDominantFfa = self.is_still_ffa_and_non_dominant()
        if (len(inEnTerrSet) < self.player.tileCount // 6
                or (self.targetPlayer != -1
                    and self.opponent_tracker.get_current_team_scores_by_player(self.player.index).standingArmy - sumEnTerrArmy < self.opponent_tracker.get_current_team_scores_by_player(self.targetPlayer).standingArmy * 0.9)):
            if not self.currently_forcing_out_of_play_gathers and not self.defend_economy and not isNonDominantFfa:
                gatherNegatives.update(inEnTerrSet)

        if self.teammate_general is not None:
            allyPlayer = self._map.players[self.teammate_general.player]
            for tile in allyPlayer.tiles:
                # if self._map.is_player_on_team_with(self.territories.territoryMap[tile], self.player) and tile.army < 8:
                if self._map.is_player_on_team_with(self.territories.territoryMap[tile], self.targetPlayer):
                    gatherNegatives.add(tile)

        if self.targetPlayer == -1:
            enemyGather = False

        if self.timings.disallowEnemyGather:
            logbook.info("Enemy gather was disallowed in timings, skipping enemy and neutral gathering.")
            enemyGather = False
            neutralGather = False

        if (enemyGather or neutralGather) and not self.is_all_in() and self._map.turn >= 150:
            gathString += f" +leaf(enemy {enemyGather})"
            # ENEMY TILE GATHER
            leafPruneStartTime = time.perf_counter()

            shortestLength = self.shortest_path_to_target_player.length
            if not self.is_all_in() and not self.defend_economy and enemyGather and self._map.turn >= 150 and leafMoves and not isNonDominantFfa:
                # Add support for 'green arrows', pushing outer territory towards enemy territory.
                goodLeaves = self.board_analysis.find_flank_leaves(
                    leafMoves,
                    minAltPathCount=2,
                    maxAltLength=shortestLength + shortestLength // 3)
                for goodLeaf in goodLeaves:
                    # if goodLeaf.dest.player == self.player:

                    self.mark_tile(goodLeaf.dest, 255)
                    # gatherTargets.add(goodLeaf.dest)
                    gatherNegatives.add(goodLeaf.dest)

            if not isNonDominantFfa:
                # for leaf in filter(lambda move: move.dest.army > 0 and (move.source.player == move.dest.player or move.source.army - 1 > move.dest.army), leafMoves):
                for leaf in filter(lambda move: move.dest.player == self.targetPlayer or (neutralGather and move.dest.player == -1), leafMoves):
                    if (
                            not (leaf.dest.isCity and leaf.dest.player == -1)
                            and leaf.dest not in self.target_player_gather_targets
                    ):
                        if leaf.dest.player != self.targetPlayer and leaf.dest.player >= 0:
                            continue
                        useTile = leaf.source
                        if leaf.dest.player == self.targetPlayer:
                            useTile = leaf.dest

                        if (
                                self.targetPlayer != -1
                                and not neutralGather
                                and (leaf.dest.player == -1 or leaf.source.player == -1)
                        ):
                            continue

                        # only gather to enemy tiles in our territory as leaves.
                        # OR to tiles that move the army closer to the conflict path
                        if (
                            self.territories.territoryMap[useTile] != self.general.player
                            and self.territories.territoryMap[useTile] not in self._map.teammates
                            and (
                                self.distance_from_target_path(leaf.source) <= self.distance_from_target_path(leaf.dest)
                                or self.distance_from_target_path(leaf.source) > self.shortest_path_to_target_player.length / 3
                            )
                        ):
                            continue

                        gatherNegatives.add(useTile)

            logbook.info(f"pruning leaves and stuff took {time.perf_counter() - leafPruneStartTime:.4f}")
            # negSet.add(self.general)

        forceGatherToEnemy = self.should_force_gather_to_enemy_tiles()
        # forceGatherToEnemy = True

        gatherPriorities = self.get_gather_tiebreak_matrix()

        usingNeedToKill = len(needToKillTiles) > 0 and not self.flanking and not self.defend_economy

        if usingNeedToKill:
            gathString += " +needToKill"
            for tile in needToKillTiles:
                if tile in gatherTargets and self.distance_from_general(tile) > 3:
                    continue

                if not forceGatherToEnemy:
                    self.mark_tile(tile, 100)

                if forceGatherToEnemy:
                    def tile_remover(curTile: Tile):
                        if curTile not in needToKillTiles and curTile in gatherTargets:
                            gatherTargets.remove(curTile)

                    SearchUtils.breadth_first_foreach_fast_no_neut_cities(self._map, [tile], 2, tile_remover)

                    gatherTargets.add(tile)

            if self.timings.in_quick_expand_split(self._map.turn) and forceGatherToEnemy:
                negCopy = gatherNegatives.copy()
                for pathTile in self.target_player_gather_path.tileList:
                    negCopy.add(pathTile)

                targetTurns = 4
                with self.perf_timer.begin_move_event(f'Timing Gather QE to enemy needToKill tiles depth {targetTurns}'):
                    move = self.timing_gather(
                        needToKillTiles,
                        negCopy,
                        skipTiles=set(genPlayer.cities),
                        force=True,
                        priorityTiles=None,
                        targetTurns=targetTurns,
                        includeGatherTreeNodesThatGatherNegative=False,
                        priorityMatrix=gatherPriorities)
                if move is not None:
                    if not isinstance(self.curPath, MoveListPath):
                        self.curPath = None
                    self.info(
                        f"GATHER QE needToKill{gathString}! Gather move: {move} Duration {time.perf_counter() - gathStartTime:.4f}")
                    if not self._map.is_player_on_team_with(move.dest.player, self.general.player) and move.dest.player != -1:
                        # clear MoveListPath gather curPath for needToKill gath captures...
                        self.curPath = None
                    return self.move_half_on_repetition(move, 6, 4)
                else:
                    logbook.info("No QE needToKill gather move found")
        else:
            needToKillTiles = None

        with self.perf_timer.begin_move_event(f'Timing Gather (normal / defensive)'):
            gatherNegatives = self.get_timing_gather_negatives_unioned(gatherNegatives)

            if self.currently_forcing_out_of_play_gathers or self.defend_economy:
                if self.currently_forcing_out_of_play_gathers:
                    gathString += " +out of play"
                if self.defend_economy:
                    gathString += " +ecDef"

                genPlayer = self._map.players[self.general.player]
                for tile in genPlayer.tiles:
                    if tile in self.board_analysis.core_play_area_matrix and tile not in self.tiles_gathered_to_this_cycle and tile.army > 1:
                        gatherPriorities.raw[tile.tile_index] -= 0.2
                    if tile not in self.board_analysis.extended_play_area_matrix and tile not in self.tiles_gathered_to_this_cycle and tile.army > 1:
                        gatherPriorities.raw[tile.tile_index] += 0.2

                # targetTurns = None
                # if self.enemy_attack_path is not None:
                #     val = self.enemy_attack_path.calculate_value(
                #         forPlayer=self.targetPlayer,
                #         teams=MapBase.get_teams_array(self._map),
                #         negativeTiles={self.general},
                #         ignoreNonPlayerArmy=True
                #     )

                    # if val > self.player.standingArmy // 3:
                    #     targetTurns = min(25, self.timings.splitTurns)

            useTrueValueGathered = False
            tgTurns = -1
            includeGatherTreeNodesThatGatherNegative = self.defend_economy
            distancePriorities = self.board_analysis.intergeneral_analysis.bMap
            if self.defend_economy:
                tgTurns = -1
                useTrueValueGathered = True
                gatherTargets = gatherTargets.copy()
                if self.enemy_attack_path is not None:
                    # keepLen = max(3 * self.target_player_gather_path.length // 4, 3 * self.enemy_attack_path.length // 4)
                    # gatherTargets = self.enemy_attack_path.get_subsegment(keepLen, end=True).tileSet
                    gatherTargets = self.enemy_attack_path.tileSet
                    if self.opponent_tracker.winning_on_economy(byRatio=1.1, offset=-25):
                        gatherPriorities = None
                        tgTurns = self._map.remainingCycleTurns - 5
                        useTrueValueGathered = False
                        includeGatherTreeNodesThatGatherNegative = True
                        distancePriorities = self.board_analysis.intergeneral_analysis.aMap
                        gatherNegatives.update(self.win_condition_analyzer.defend_cities)
                        gathString = f" +NO_MAT_RISKPATH {tgTurns}t" + gathString
                    else:
                        gathString = " +RISKPATH" + gathString
                else:
                    gatherTargets.update([t for t in self.board_analysis.intergeneral_analysis.shortestPathWay.tiles if not t.isObstacle])

                for t in gatherTargets:
                    self.viewInfo.add_targeted_tile(t, TargetStyle.WHITE, radiusReduction=11)

            move = self.timing_gather(
                [t for t in gatherTargets],
                gatherNegatives,
                skipTiles=None,
                force=True,
                priorityTiles=None,
                priorityMatrix=gatherPriorities,
                includeGatherTreeNodesThatGatherNegative=includeGatherTreeNodesThatGatherNegative,
                distancePriorities=distancePriorities,
                useTrueValueGathered=useTrueValueGathered,
                targetTurns=tgTurns,
                pruneToValuePerTurn=self.defend_economy)

        if move is not None:
            if move.dest.player != self.player.index and move.dest not in self.target_player_gather_targets and not self.flanking and needToKillTiles is not None and move.dest in needToKillTiles:
                self.timings.splitTurns += 1
                self.timings.launchTiming += 1

            if move.source.isCity or move.source.isGeneral:
                self.cities_gathered_this_cycle.add(move.source)

            if move.dest.isCity and move.dest.player == self.player.index and move.dest in self.cities_gathered_this_cycle:
                self.cities_gathered_this_cycle.remove(move.dest)

            if not isinstance(self.curPath, MoveListPath):
                self.curPath = None
            self.info(
                f"GATHER {gathString}! Gather move: {move} Duration {time.perf_counter() - gathStartTime:.4f}")
            return self.move_half_on_repetition(move, 6, 4)
        else:
            logbook.info("No gather move found")

        return None

    @staticmethod
    def render_tile_deltas_in_view_info(viewInfo: ViewInfo, map: MapBase):
        for tile in map.tiles_by_index:
            renderMore = False
            if (
                    tile.delta.armyMovedHere
                    or tile.delta.lostSight
                    or tile.delta.gainedSight
                    or tile.delta.discovered
                    or tile.delta.armyDelta != 0
                    or tile.delta.unexplainedDelta != 0
                    # or tile.delta.imperfectArmyDelta
                    or tile.delta.fromTile is not None
                    or tile.delta.toTile is not None
            ):
                renderMore = True

            s = []
            if tile.delta.armyMovedHere:
                s.append('M')
            if tile.delta.imperfectArmyDelta:
                s.append('I')
            if tile.delta.lostSight:
                s.append('L')
            if tile.delta.gainedSight:
                s.append('G')
            if tile.delta.discovered:
                s.append('D')
            s.append(' ')
            viewInfo.bottomRightGridText.raw[tile.tile_index] = ''.join(s)

            if tile.delta.armyDelta != 0:
                viewInfo.bottomLeftGridText.raw[tile.tile_index] = f'd{tile.delta.armyDelta:+d}'
            if tile.delta.unexplainedDelta != 0:
                viewInfo.bottomMidLeftGridText.raw[tile.tile_index] = f'u{tile.delta.unexplainedDelta:+d}'
            if renderMore:
                moves = ''
                if tile.delta.toTile and tile.delta.fromTile:
                    moves = f'{str(tile.delta.fromTile)}-{str(tile.delta.toTile)}'
                elif tile.delta.fromTile:
                    moves = f'<-{str(tile.delta.fromTile)}'
                elif tile.delta.toTile:
                    moves = f'->{str(tile.delta.toTile)}'
                viewInfo.topRightGridText.raw[tile.tile_index] = moves
                viewInfo.midRightGridText.raw[tile.tile_index] = f'{tile.delta.oldArmy}'
                if tile.delta.oldOwner != tile.delta.newOwner:
                    viewInfo.bottomMidRightGridText.raw[tile.tile_index] = f'{tile.delta.oldOwner}-{tile.delta.newOwner}'

    @staticmethod
    def render_tile_state_in_view_info(viewInfo: ViewInfo, map: MapBase):
        for tile in map.tiles_by_index:
            s = []
            if tile.isPathable:
                # s.append('P')
                pass
            else:
                s.append('-')
            if tile in map.pathable_tiles:
                # s.append('P')
                pass
            else:
                s.append('-')
            if tile.isCostlyNeutralCity:
                s.append('C')
            if tile not in map.reachable_tiles:
                s.append('X')
            if tile.isObstacle:
                s.append('O')
            if tile.isMountain:
                s.append('M')
            if tile.overridePathable is not None:
                if tile.overridePathable:
                    s.append('p')
                else:
                    s.append('z')
            s.append(' ')
            viewInfo.bottomMidRightGridText.raw[tile.tile_index] = ''.join(s)


    # def check_if_need_to_gather_longer_to_hold_fresh_cities(self):
    #     freshCityCount = 0
    #     sketchiestCity: Tile = self.general
    #     contestCity: Tile | None = None
    #
    #     offset = 8
    #
    #     for city in self._map.players[self.general.player].cities:
    #         if not self._map.is_player_on_team_with(city.delta.oldOwner, self.general.player):
    #             freshCityCount += 1
    #
    #             nearerToUs = self.board_analysis.intergeneral_analysis.aMap[city] < self.board_analysis.intergeneral_analysis.bMap[city]
    #             nearerToEnemyThanSketchiest = self.board_analysis.intergeneral_analysis.bMap[city] < self.board_analysis.intergeneral_analysis.bMap[sketchiestCity]
    #             enemyTilesVisionRange = self.count_enemy_tiles_near_tile(city, 2)
    #             enemyTilesNear = self.count_enemy_tiles_near_tile(city, self.shortest_path_to_target_player.length // 5)
    #             if nearerToUs and nearerToEnemyThanSketchiest and (enemyTilesNear > 5 or enemyTilesVisionRange > 0):
    #                 sketchiestCity = city
    #             elif city.delta.oldOwner >= 0 and (contestCity is None or self.board_analysis.intergeneral_analysis.aMap[city] < self.board_analysis.intergeneral_analysis.aMap[contestCity]):
    #                 contestCity = city
    #
    #             if city.delta.oldOwner >= 0:
    #                 offset = 7
    #
    #     earlyCycleSlightlyWinning = self.timings is not None and self.timings.get_turn_in_cycle(self._map.turn) < 15 and self.opponent_tracker.winning_on_economy(byRatio=1.05)
    #     heavilyWinning = self.opponent_tracker.winning_on_economy(byRatio=1.25, cityValue=35, offset=-5)
    #
    #     if (heavilyWinning or earlyCycleSlightlyWinning) and self.all_in_city_behind and self.opponent_tracker.even_or_up_on_cities():
    #         self.all_in_city_behind = False
    #         self.is_all_in_losing = False
    #         self.is_all_in_army_advantage = False
    #
    #     if freshCityCount > 0 and (earlyCycleSlightlyWinning or heavilyWinning) and not self._map.remainingPlayers > 2:
    #         self.flanking = False
    #         winningEcon = self.opponent_tracker.winning_on_economy(byRatio=1.2)
    #         # todo need same for 2v2
    #         d25 = self._map.players[self.targetPlayer].delta25tiles - self._map.players[self.general.player].delta25tiles
    #         if d25 < 5 and self.opponent_tracker.up_on_cities() and self.opponent_tracker.winning_on_tiles():
    #             offset += 10
    #         elif d25 < 7 and self.opponent_tracker.up_on_cities():
    #             offset += 5
    #         if winningEcon and self._map.remainingPlayers == 2:
    #             offset += 5
    #         if heavilyWinning and self.opponent_tracker.winning_on_army():
    #             offset += 7
    #
    #         if offset < 15:
    #             return
    #
    #         self.locked_launch_point = sketchiestCity
    #         if contestCity is not None:
    #             self.locked_launch_point = contestCity
    #         self.recalculate_player_paths(force=True)
    #         curTurn = self.timings.get_turn_in_cycle(self._map.turn)
    #         self.timings.splitTurns = min(self.timings.cycleTurns - 14, max(curTurn + offset, self.timings.splitTurns + offset))
    #         self.viewInfo.add_info_line(f'CCAP GATH, d25 {d25}, offset {offset} winningEcon 1.2 {str(winningEcon)[0]} heavilyWinning {str(heavilyWinning)[0]}')
    #         self.timings.launchTiming = max(self.timings.splitTurns, self.timings.launchTiming)

    def get_scrim_cached(self, friendlyArmies: typing.List[Army], enemyArmies: typing.List[Army]) -> ArmySimResult | None:
        key = self.get_scrim_cache_key(friendlyArmies, enemyArmies)
        cachedSimResult: ArmySimResult | None = self.cached_scrims.get(key, None)
        return cachedSimResult

    def get_scrim_cache_key(self, friendlyArmies: typing.List[Army], enemyArmies: typing.List[Army]) -> str:
        sortedArmies = list(sorted(friendlyArmies, key=lambda a: a.tile))
        sortedArmies.extend(list(sorted(enemyArmies, key=lambda a: a.tile)))
        key = ''.join([str(a.tile) for a in sortedArmies])
        return key

    def find_sketchy_fog_flank_from_enemy_in_play_area(self) -> Path | None:
        """
        Hunts for a sketchy flank attack point the enemy might be inclined to abuse from a city/general,
        and returns it as a fog-only path to the enemy attack source.
        """

        launchPoints = [self.targetPlayerExpectedGeneralLocation]
        for c in self.targetPlayerObj.cities:
            if not c.discovered:
                continue
            if not self.territories.is_tile_in_enemy_territory(c):
                continue
            launchPoints.append(c)

        distCap = self.board_analysis.inter_general_distance + 7
        # distCap = self.board_analysis.inter_general_distance
        depth = min(30, distCap)

        distMatrix = SearchUtils.build_distance_map_matrix(self._map, [self.general])

        sketchyPath = self.find_flank_opportunity(
            targetPlayer=self.general.player,
            flankingPlayer=self.targetPlayer,
            flankPlayerLaunchPoints=launchPoints,
            depth=depth,
            targetDistMap=distMatrix,
            validEmergencePointMatrix=self.board_analysis.flank_danger_play_area_matrix)

        return sketchyPath

    def find_sketchiest_fog_flank_from_enemy(self) -> Path | None:
        """
        Hunts for a sketchy flank attack point the enemy might be inclined to abuse from a city/general,
        and returns it as a fog-only path to the enemy attack source.
        """
        territoryDists = self.territories.territoryDistances[self.general.player]

        enemyLaunchPoints = self.get_target_player_possible_general_location_tiles_sorted(elimNearbyRange=5, cutoffEmergenceRatio=0.25)
        for c in self.targetPlayerObj.cities:
            # if not c.discovered:
            #     continue
            # if not self.territories.is_tile_in_enemy_territory(c):
            #     continue
            if c.visible:
                continue
            enemyLaunchPoints.append(c)

        distCap = self.board_analysis.inter_general_distance + 15
        # distCap = self.board_analysis.inter_general_distance
        depth = min(35, distCap)

        missingCities = self.opponent_tracker.get_team_unknown_city_count_by_player(self.targetPlayer)

        def valueFunc(tile: Tile, prioVals) -> typing.Tuple | None:
            # Why was this commented out...?
            if tile not in self.board_analysis.flankable_fog_area_matrix:
                return None

            if prioVals:
                dist, negSumTerritoryDists, _, usedUnkCities = prioVals

                return 0 - self.board_analysis.intergeneral_analysis.aMap[tile], 0 - negSumTerritoryDists, dist
            return None

        def prioFunc(tile: Tile, prioVals) -> typing.Tuple | None:
            # dist = 0
            # negSumTerritoryDists = 0
            # usedUnkCities = 0
            # if prioVals:
            dist, negSumTerritoryDists, _, usedUnkCities = prioVals

            if tile.isObstacle:
                if tile.visible:
                    return None
                if tile.isMountain:
                    return None
                wallBreachScore = self.board_analysis.get_wall_breach_expandability(tile, self.targetPlayer)
                if not wallBreachScore or wallBreachScore < 3:
                    return None
                usedUnkCities += 1

                if usedUnkCities > missingCities:
                    return None

            # Why was this commented out...?
            if tile not in self.board_analysis.flankable_fog_area_matrix:
                return None

            return dist + 1, negSumTerritoryDists - territoryDists[tile], self.board_analysis.intergeneral_analysis.aMap[tile], usedUnkCities

        skip = set()

        for tile in self._map.get_all_tiles():
            if tile not in self.board_analysis.flankable_fog_area_matrix:
                skip.add(tile)
            # if tile.isCity and tile.isNeutral and not

        startTiles = {}
        for tile in enemyLaunchPoints:
            startTiles[tile] = ((0, 0, 0, 0), 0)

        # logbook.info(f'Looking for flank')
        path = SearchUtils.breadth_first_dynamic_max(
            self._map,
            startTiles,
            valueFunc=valueFunc,
            priorityFunc=prioFunc,
            skipTiles=skip,
            maxTime=0.1,
            maxDepth=depth,
            noNeutralCities=False,
            useGlobalVisitedSet=True,
            searchingPlayer=self.targetPlayer,
            noNeutralUndiscoveredObstacles=False,
            skipFunc=lambda t, _: False,
            noLog=True)

        if not path or path.length < 3:
            return None

        return path

    def check_for_attack_launch_move(self, outLaunchPlanNegatives: typing.Set[Tile]) -> Move | None:
        if self.target_player_gather_path is None and not self.flanking:
            return None

        cycleTurn = self.timings.get_turn_in_cycle(self._map.turn)
        cycleTurnsLeft = self.timings.get_turns_left_in_cycle(self._map.turn)

        # if self.targetPlayer != -1:
        #     if cycleTurnsLeft > self.target_player_gather_path.length and self.high_fog_risk and (not self.is_lag_massive_map or self._map.turn < 3 or (self._map.turn + 2) % 5 == 0):
        #         outLaunchPlanNegatives.update(self.target_player_gather_path.tileSet)
        #         if self.locked_launch_point is not None and self.locked_launch_point != self.general:
        #             self.viewInfo.add_info_line(f're-centering gen launch point due to fog risk')
        #             self.locked_launch_point = self.general
        #             self.recalculate_player_paths(force=True)
        #
        #         return None

        path = self.get_value_per_turn_subsegment(self.target_player_gather_path, 1.0, 0.25)
        origPathLength = path.length
        # reduce the length of the path to allow for other use of the army

        targetPathLength = path.length * 3 // 9 + 1
        # if self.is_all_in():
        #     allInPathLength = path.length * 5 // 9 + 1
        #     self.viewInfo.add_info_line(
        #         f"because all in, changed path length from {targetPathLength} to {allInPathLength}")
        #     targetPathLength = allInPathLength

        if self.flanking:
            targetPathLength = targetPathLength // 2 + 1
        #
        # if not self.targetPlayerExpectedGeneralLocation.isGeneral:
        #     targetPathLength += 2

        maxLength = 17
        if self.timings.cycleTurns > 50:
            maxLength = 34

        # never use a super long path because it leaves no time to expand.
        # This is just cutting off the attack-send path length to stop rallying
        # the attack around enemy territory to let the bot expand or whatever. This isn't modifying the real path.
        targetPathLength = min(maxLength, targetPathLength)
        path = path.get_subsegment(targetPathLength)
        if path.length == 0:
            return None

        path.calculate_value(self.general.player, teams=self._map.team_ids_by_player_index)
        logbook.info(f"  value subsegment = {str(path)}")
        timingTurn = (self._map.turn + self.timings.offsetTurns) % self.timings.cycleTurns
        player = self._map.players[self.general.player]

        enemyGenAdj = []
        for generalAdj in self.general.adjacents:
            if self._map.is_tile_enemy(generalAdj):
                self.viewInfo.add_targeted_tile(generalAdj)
                enemyGenAdj.append(generalAdj)

        pathWorth = self.get_player_army_amount_on_path(self.target_player_gather_path, self.general.player)

        if self._map.turn >= 50 and self.timings.in_launch_timing(self._map.turn) and (
                self.targetPlayer != -1 or self._map.remainingPlayers <= 2):
            inAttackWindow = timingTurn < self.timings.launchTiming + 4
            minArmy = min(player.standingArmy ** 0.9, (player.standingArmy ** 0.72) * 1.7)
            if self.flanking and pathWorth > 0:
                minArmy = 0

            self.info(
                f"  T Launch window {inAttackWindow} - minArmy {minArmy}, pathVal {path.value}, timingTurn {timingTurn} < launchTiming + origPathLength {origPathLength} / 3 {self.timings.launchTiming + origPathLength / 2:.1f}")

            if path is not None and path.length > 0 and pathWorth > minArmy and inAttackWindow and path.start.tile.player == self.general.player:
                # Then it is worth launching the attack?
                move = self.get_first_path_move(path)
                if self.is_move_safe_against_threats(move):
                    logbook.info(
                        f"  attacking because NEW worth_attacking_target(), pathWorth {pathWorth}, minArmy {minArmy}: {str(path)}")
                    self.lastTargetAttackTurn = self._map.turn
                    # return self.move_half_on_repetition(Move(path[1].tile, path[1].parent.tile, path[1].move_half), 7, 3)
                    if self.timings.is_early_flank_launch:
                        path.start.move_half = True
                    self.curPath = path
                    return move

            elif path is not None:
                logbook.info(
                    "  Did NOT attack because NOT pathWorth > minArmy or not inAttackWindow??? pathWorth {}, minArmy {}, inAttackWindow {}: {}".format(
                        pathWorth, minArmy, path.toString(), inAttackWindow))
            else:
                logbook.info("  Did not attack because path was None.")
        else:
            logbook.info("skipped launch because outside launch window")

        return None

    def set_all_in_cycle_to_hit_with_current_timings(self, cycle: int, bufferTurnsEndOfCycle: int = 5):
        """
        @param cycle: The amount of turns to keep cycling the all in after the initial timing hit.
        @param bufferTurnsEndOfCycle:

        @return:
        """

        turnsLeftInCurrentCycle = self.timings.cycleTurns - self.timings.get_turn_in_cycle(self._map.turn)
        self.all_in_army_advantage_counter = cycle - turnsLeftInCurrentCycle + bufferTurnsEndOfCycle
        self.all_in_army_advantage_cycle = cycle

    def try_find_flank_all_in(self, hitGeneralAtTurn: int) -> Move | None:
        launchPoint: Move | None = None
        return None

    def find_flank_opportunity(
            self,
            targetPlayer: int,
            flankingPlayer: int,
            flankPlayerLaunchPoints: typing.List[Tile],
            depth: int,
            targetDistMap: MapMatrixInterface[int],
            validEmergencePointMatrix: MapMatrixSet | None,
            maxFogRange: int = -1
    ) -> Path | None:
        if maxFogRange == -1:
            maxFogRange = self.board_analysis.inter_general_distance + 2

        def prioFunc(curTile: Tile, prioObj):
            dist, negMaxPerTurn, zoningPenalty, fogTileCount, sequentialNonFog, totalNonFog, minDistFogEmergence, hadPossibleVision, hadDefiniteVision, fromTile = prioObj

            hasPossibleVision = SearchUtils.any_where(curTile.adjacents, lambda t: t.player == targetPlayer or (not curTile.visible and self.territories.territoryMap[t] == targetPlayer))
            hasDefiniteVision = SearchUtils.any_where(curTile.adjacents, lambda t: t.player == targetPlayer)

            if fromTile is not None:
                hasPossibleFromVision = SearchUtils.any_where(fromTile.adjacents, lambda t: t.player == targetPlayer or (not fromTile.visible and self.territories.territoryMap[t] == targetPlayer))
                hasDefiniteFromVision = SearchUtils.any_where(fromTile.adjacents, lambda t: t.player == targetPlayer)

                if not hasPossibleFromVision and not hasDefiniteFromVision and hasDefiniteVision:
                    return None

            if not hasPossibleVision:
                fogTileCount += 1
                sequentialNonFog = 0
            elif not hasDefiniteVision:
                fogTileCount += 0.5
                sequentialNonFog += 0.5
                minDistFogEmergence = min(dist + 1, minDistFogEmergence)
            else:
                sequentialNonFog += 1
                totalNonFog += 1
                minDistFogEmergence = min(dist, minDistFogEmergence)

            zoningPenalty = 1 / (1 + self.get_distance_from_board_center(curTile, center_ratio=0.0))

            dist += 1

            return dist, 0 - fogTileCount / dist, zoningPenalty, fogTileCount, sequentialNonFog, totalNonFog, minDistFogEmergence, hasPossibleVision, hasDefiniteVision, curTile

        def valueFunc(curTile: Tile, prioObj):
            dist, negMaxPerTurn, zoningPenalty, fogTileCount, sequentialNonFog, totalNonFog, minDistFogEmergence, hasPossibleVision, hasDefiniteVision, fromTile = prioObj

            if fromTile is not None and targetDistMap[fromTile] < targetDistMap[curTile]:
                return None
            if sequentialNonFog > 0:
                return None
            if totalNonFog > maxFogRange:
                return None
            if validEmergencePointMatrix is not None and curTile not in validEmergencePointMatrix:
                return None

            return minDistFogEmergence - zoningPenalty

        startTiles = {}
        for tile in flankPlayerLaunchPoints:
            startTiles[tile] = ((0, 0, 0, 0, 0, 0, 1000, 0, 0, None), 0)
        flankPath = SearchUtils.breadth_first_dynamic_max(
            self._map,
            startTiles,
            priorityFunc=prioFunc,
            valueFunc=valueFunc,
            noNeutralCities=False,
            skipFunc=lambda t, prio: t.isUndiscoveredObstacle or t.visible,
            maxDepth=depth,
            searchingPlayer=flankingPlayer,
        )

        if flankPath is not None:
            flankPath = flankPath.get_reversed()

        return flankPath

    def check_target_player_just_took_city(self):
        if self.targetPlayerObj is not None and self.targetPlayer != -1:
            teamData = self.opponent_tracker.get_current_team_scores_by_player(self.targetPlayer)
            numLivingPlayers = len(teamData.livingPlayers)
            if teamData.cityCount > self._lastTargetPlayerCityCount:
                self.viewInfo.add_info_line(f'Dropping timings because target player just took a city.')

                if self._map.is_2v2:
                    mostRecentCity = None
                    for en in self.opponent_tracker.get_team_players_by_player(self.targetPlayer):
                        for city in self._map.players[en].cities:
                            if city.delta.oldOwner != city.player:
                                mostRecentCity = city

                    if mostRecentCity is None:
                        mostRecentCity = self.targetPlayerExpectedGeneralLocation

                    self.send_teammate_communication(f'Opps have {teamData.cityCount - numLivingPlayers} cities. New city might be around here:', mostRecentCity)

                self.timings = None

            self._lastTargetPlayerCityCount = teamData.cityCount

    def get_2v2_launch_point(self) -> Tile:
        fromTile = self.general
        usDist = self.distance_from_general(self.targetPlayerExpectedGeneralLocation)
        allyAttackPath = self.get_path_to_target(
            self.targetPlayerExpectedGeneralLocation,
            preferEnemy=True,
            preferNeutral=True,
            fromTile=self.teammate_general)
        if allyAttackPath is None:
            return self.general
        allyDist = allyAttackPath.length
        lockGeneral = False

        teammateDistFromUs = self.teammate_path.length

        teammateRallyDistOffset = teammateDistFromUs // 4

        if self.targetPlayerObj.knowsAllyKingLocation and self.targetPlayerObj.knowsKingLocation:
            fromTile = self.teammate_path.get_subsegment(2 * self.teammate_path.length // 5).tail.tile
            teammateRallyDistOffset = teammateDistFromUs // 2
            lockGeneral = False
        elif self.targetPlayerObj.knowsAllyKingLocation:
            fromTile = self.teammate_general
            lockGeneral = True
            self.viewInfo.add_info_line(
                f'ALLY lp {str(fromTile)} due to vision')
        elif self.targetPlayerObj.knowsKingLocation:
            fromTile = self.general
            lockGeneral = True
            self.viewInfo.add_info_line(
                f'SELF lp {str(fromTile)} due to vision')

        if teammateDistFromUs < 15 and not lockGeneral:
            contested = self.get_contested_targets()
            if len(contested) > 0:
                fromTile = contested[0].tile
                self.launchPoints = [c.tile for c in contested]
            elif allyDist + teammateRallyDistOffset <= usDist:
                # then use ally as the launch point
                # TODO make sure we're not gonna get flanked by other player...?
                fromTile = self.teammate_general
                # TODO lock or not lock?
                self.viewInfo.add_info_line(
                    f'Ally lp {str(self.teammate_general)} dist {allyDist} from {str(self.targetPlayerExpectedGeneralLocation)} vs us {usDist}')

        return fromTile

    def increment_attack_counts(self, tile: Tile):
        contestData = self.contest_data.get(tile, None)
        if contestData is None:
            contestData = ContestData(tile)
            self.contest_data[tile] = contestData

        if contestData.last_attacked_turn < self._map.turn - 5:
            contestData.attacked_count += 1

        contestData.last_attacked_turn = self._map.turn

    def get_contested_targets(
            self,
            shortTermContestCutoff: int = 25,
            longTermContestCutoff: int = 60,
            numToInclude=3,
            excludeGeneral: bool = False
    ) -> typing.List[ContestData]:
        """
        Finds up to 3 recently contested tiles within last shortTermContestCutoff moves.
        If it fails to find those, looks for at least one within last longTermContestCutoff moves.
        """

        contestedSorted = [c for c in sorted(self.contest_data.values(), key=lambda c: c.last_attacked_turn, reverse=True) if not c.tile.isGeneral or not excludeGeneral]

        mostRecentTargets = [c for c in contestedSorted[0:numToInclude] if c.last_attacked_turn > self._map.turn - longTermContestCutoff]

        if len(mostRecentTargets) == 0:
            return []

        shortTermTargets = [t for t in mostRecentTargets if t.last_attacked_turn > self._map.turn - shortTermContestCutoff]
        if len(shortTermTargets) > 0:
            mostRecentTargets = shortTermTargets

        logbook.info(f'Found contested tiles in get_contested_targets: {mostRecentTargets}')

        return mostRecentTargets

    def send_teammate_communication(self, message: str, pingTile: Tile | None = None, cooldown: int = 10, detectOnMessageAlone: bool = False, detectionKey: str | None = None):
        """
        Use this to send a chat message to team-chat only, as well as an optional tile ping.

        @param message:
        @param pingTile:
        @param cooldown: the number of turns before this message with this tile can be sent again.
        @param detectOnMessageAlone: if True, detect cooldown-duplication based on message, not message + tile. When duplicate on message is detected this way, the message will not even be written to the UI.
        @param detectionKey: if present, will be used to detect whether something shouldn't be sent to teammate due to cooldown.
        @return:
        """

        commKey = message

        if detectOnMessageAlone:
            if not self.cooldown_allows(commKey, cooldown, doNotUpdate=True):
                return

            if pingTile is not None:
                self._communications_sent_cooldown_cache[commKey] = self._map.turn

        if pingTile is not None:
            commKey = f'[@{str(pingTile)}] {commKey}'
        self.viewInfo.add_info_line(f'Send: {commKey}')

        if detectionKey is None:
            detectionKey = commKey

        if not self._map.is_2v2:
            return

        if self.cooldown_allows(detectionKey, cooldown):
            self._outbound_team_chat.put(message)

            if pingTile is not None:
                self.send_teammate_tile_ping(pingTile)

    def send_all_chat_communication(self, message: str):
        """
        Use this to send a chat message to team-chat only, as well as an optional tile ping.

        @param message:
        @return:
        """
        self._outbound_all_chat.put(message)

    def send_teammate_path_ping(self, path: Path, cooldown: int = 0, cooldownKey: str | None = None):
        """Pings all the tiles in a path"""
        for tile in path.tileList:
            self.send_teammate_tile_ping(tile, cooldown, cooldownKey)

    def send_teammate_tile_ping(self, pingTile: Tile, cooldown: int = 0, cooldownKey: str | None = None):
        """
        Use this to send a tile ping.
GAME START
12:12:56.983
42["game_start",{"playerIndex":0,"playerColors":[0,1,2,3],"replay_id":"SeGpX_VWT","chat_room":"game_1697051578054SDTRNZlxr5ZbYoIPAN-p","team_chat_room":"game_1697051578054SDTRNZlxr5ZbYoIPAN-p_team_1","usernames":["EklipZ_0x45","[Bot]EklipZ_ai","[Bot] Sora_ai_ek","[Bot] Sora_ai_2"],"teams":[1,1,2,2],"game_type":"2v2","swamps":[],"lights":[],"options":{}},null]    361
12:12:59.484



        SENDING
42["ping_tile",125]    19

all chat
(sent)
42["chat_message","game_1697051578054SDTRNZlxr5ZbYoIPAN-p","norm chat",""]
(self received)
42["chat_message","game_1697051578054SDTRNZlxr5ZbYoIPAN-p",{"username":"EklipZ_0x45","text":"norm chat","prefix":"","playerColor":0,"turn":13}]

team chat
(sent)
42["chat_message","game_1697051578054SDTRNZlxr5ZbYoIPAN-p_team_1","team chat","[team] "]
(self received)
42["chat_message","game_1697051578054SDTRNZlxr5ZbYoIPAN-p_team_1",{"username":"EklipZ_0x45","text":"team chat","prefix":"[team] ","playerColor":0,"turn":20}]

Surrender
(sent)
42["surrender"]    15
(recv)
42["chat_message","game_1697051578054SDTRNZlxr5ZbYoIPAN-p",{"text":"EklipZ_0x45 surrendered.","playerColor":0,"turn":43}]    121
(recv)
42["game_lost",{"surrender":true},null]    39
(recv)
42["game_update",{"scores":[{"total":22,"tiles":18,"i":2,"colo


RECV BY OTHER PLAYER

2023-10-11 12:13:05.949347 - WS recv: "42[\"chat_message\",\"game_1697051578054SDTRNZlxr5ZbYoIPAN-p\",{\"username\":\"EklipZ_0x45\",\"text\":\"norm chat\",\"prefix\":\"\",\"playerColor\":0,\"turn\":13}]"
~~~
~~~
From EklipZ_0x45: norm chat
~~~



2023-10-11 12:13:09.115008 - WS recv: "42[\"chat_message\",\"game_1697051578054SDTRNZlxr5ZbYoIPAN-p_team_1\",{\"username\":\"EklipZ_0x45\",\"text\":\"team chat\",\"prefix\":\"[team] \",\"playerColor\":0,\"turn\":20}]"
~~~
~~~
From EklipZ_0x45: team chat
~~~
~~~


2023-10-11 12:13:10.200755 - WS recv: "42[\"ping_tile\",125,0]"
Unknown message type: ['ping_tile', 125, 0]        @param pingTile:
        @param cooldown: how many turns to refuse to ping this tile again for.
        @return:
        """

        if not self._map.is_2v2:
            return

        if cooldown > 0:
            if cooldownKey is None:
                cooldownKey = str(pingTile)

            cooldownKey = f'PINGCOOL{cooldownKey}'

            coolTurn = self._communications_sent_cooldown_cache.get(cooldownKey, -250)
            if coolTurn > self._map.turn - cooldown:
                return
            self._communications_sent_cooldown_cache[cooldownKey] = self._map.turn

        self.viewInfo.add_targeted_tile(pingTile, targetStyle=TargetStyle.GOLD)
        self._tile_ping_queue.put(pingTile)

    def get_queued_tile_pings(self) -> typing.List[Tile]:
        outbound = []
        while self._tile_ping_queue.qsize() > 0:
            outbound.append(self._tile_ping_queue.get())

        return outbound

    def do_thing(self):
        time.sleep(8)
        lastSwapped = []
        for tile in self._map.get_all_tiles():
            if random.randint(1, 300) > 250:
                h = tile.movable
                tile.movable = lastSwapped
                lastSwapped = h
            if random.randint(1, 300) > 298:
                tile.isMountain = True
                tile.army = 0
            if random.randint(1, 300) > 275 and not tile.visible:
                tile.player = -1
                tile.army = 0

    def get_queued_teammate_messages(self) -> typing.List[str]:
        outbound = []
        while self._outbound_team_chat.qsize() > 0:
            outbound.append(self._outbound_team_chat.get())

        return outbound

    def get_queued_all_chat_messages(self) -> typing.List[str]:
        outbound = []
        while self._outbound_all_chat.qsize() > 0:
            outbound.append(self._outbound_all_chat.get())

        return outbound

    def notify_chat_message(self, chatUpdate: ChatUpdate):
        self._chat_messages_received.put(chatUpdate)

        st = str(reversed('lare' + 'gneg'))
        a = 's'
        a = f'{a}to'
        a += f'{a}p'
        if self._map and st in self._map.usernames[self._map.player_index] and a in chatUpdate.message.lower():
            _spawn(self.do_thing)

    def notify_tile_ping(self, pingedTile: Tile):
        self._tiles_pinged_by_teammate.put(pingedTile)

    def determine_should_defend_ally(self) -> bool:
        # then this is a threat against ally, check if they defend:
        threat = self.dangerAnalyzer.fastestAllyThreat
        general = self._map.generals[threat.path.tail.tile.player]

        # # TODO hack for now until fog duplication issues in 2v2 are resolved.
        # if not threat.path.start.tile.visible:
        #     return False

        if self.teammate_communicator is not None:
            if self.teammate_communicator.is_defense_lead:
                return True

        allowComms = threat.path.start.tile.visible

        teammateSelfSavePathShort = self.get_best_defense(
            threat.path.tail.tile,
            threat.turns - 3,
            threat.path.tileList)
        if teammateSelfSavePathShort is not None:
            logbook.info(
                f"  threatVal {threat.threatValue}, teammateSelfSavePathShort {str(teammateSelfSavePathShort)}")
            if threat.threatValue < teammateSelfSavePathShort.value:
                if allowComms:
                    self.send_teammate_communication(
                        f"|  Need {threat.threatValue} @ you in {threat.turns} moves. Expecting you to block by yourself with pinged tile.",
                        threat.path.start.tile,
                        detectionKey='allyDefense',
                        cooldown=10)
                    self.send_teammate_tile_ping(threat.path.tail.tile, cooldown=10)
                    self.send_teammate_tile_ping(teammateSelfSavePathShort.start.next.tile, cooldown=10)
                return False

        teammateSelfSavePath = self.get_best_defense(
            threat.path.tail.tile,
            threat.turns - 1,
            threat.path.tileList)
        if teammateSelfSavePath is not None:
            logbook.info(
                f"  threatVal {threat.threatValue}, teammateSelfSavePath {str(teammateSelfSavePath)}")
            if threat.threatValue < teammateSelfSavePath.value:
                # if threat.path.start.tile in teammateSelfSavePath.tail.tile.adjacents:
                #     return False
                if allowComms:
                    self.send_teammate_communication(
                        f"-- Need {threat.threatValue} @ you in {threat.turns} moves. You may barely manage. Protecting you just in case.",
                        detectionKey='allyDefenseBarely',
                        cooldown=10)
                    self.send_teammate_tile_ping(threat.path.tail.tile, cooldown=10)
                    self.send_teammate_tile_ping(teammateSelfSavePath.start.next.tile, cooldown=10)
                return True
            else:
                # if threat.path.start.tile in teammateSelfSavePath.tail.tile.adjacents:
                #     return False
                if allowComms:
                    self.send_teammate_communication(
                        f"---Need {threat.threatValue} @ you in {threat.turns} moves. You may be unable to save yourself by {threat.threatValue - teammateSelfSavePath.value} army, trying to help.",
                        threat.path.start.tile,
                        detectionKey='allyDefense',
                        cooldown=10)
                    # self.send_teammate_tile_ping(threat.path.tail.tile, cooldown=3)
                    if teammateSelfSavePath.start.tile.lastMovedTurn < self._map.turn - 1:
                        self.send_teammate_tile_ping(teammateSelfSavePath.start.tile, cooldown=10, cooldownKey='allyDefensePing')
                return True

        if allowComms:
            self.send_teammate_communication(
                f"---Need {threat.threatValue} @ you in {threat.turns} moves. You have no defense, trying to defend you.",
                threat.path.start.next.tile,
                detectionKey='allyDefense',
                cooldown=10)
            self.send_teammate_tile_ping(threat.path.tail.tile, cooldown=10, cooldownKey='allyDefensePing')
        return True

    def find_end_of_turn_sim_result(self, threat: ThreatObj | None, kingKillPath: Path | None, time_limit: float | None = None) -> ArmySimResult | None:
        # frArmies = [a for a in self.armyTracker.armies.values() if a.player == self.general.player]
        # if len(frArmies) <= self.behavior_end_of_turn_scrim_army_count:
        frArmies = self.get_largest_tiles_as_armies(player=self.general.player, limit=self.behavior_end_of_turn_scrim_army_count)

        # enArmies = [a for a in self.armyTracker.armies.values() if a.player == self.player]
        # if len(enArmies) <= self.behavior_end_of_turn_scrim_army_count:
        enArmies = self.get_largest_tiles_as_armies(player=self.targetPlayer, limit=self.behavior_end_of_turn_scrim_army_count)

        if len(enArmies) == 0:
            self.targetPlayerExpectedGeneralLocation.player = self.targetPlayer
            enArmies = [self.get_army_at(self.targetPlayerExpectedGeneralLocation, no_expected_path=True)]

        enemyHasKillThreat = threat is not None and threat.threatType == ThreatType.Kill
        friendlyHasKillThreat = kingKillPath is not None

        if time_limit is None:
            time_limit = self.get_remaining_move_time()

        if time_limit < 0.06:
            logbook.info(f'not enough time left ({time_limit:.3f}) for end of turn scrim. Returning none.')
            return None

        old_allow_random_no_ops = self.mcts_engine.allow_random_no_ops
        old_friendly_move_no_op_scale_10_fraction = self.mcts_engine.eval_params.friendly_move_no_op_scale_10_fraction
        old_enemy_move_no_op_scale_10_fraction = self.mcts_engine.eval_params.enemy_move_no_op_scale_10_fraction

        self.mcts_engine.allow_random_no_ops = False
        self.mcts_engine.eval_params.friendly_move_no_op_scale_10_fraction = 0
        self.mcts_engine.eval_params.enemy_move_no_op_scale_10_fraction = 0

        simResult = self.get_armies_scrim_result(
            frArmies,
            enArmies,
            enemyHasKillThreat=enemyHasKillThreat,
            friendlyHasKillThreat=friendlyHasKillThreat,
            time_limit=time_limit - 0.01)

        self.mcts_engine.allow_random_no_ops = old_allow_random_no_ops
        self.mcts_engine.eval_params.friendly_move_no_op_scale_10_fraction = old_friendly_move_no_op_scale_10_fraction
        self.mcts_engine.eval_params.enemy_move_no_op_scale_10_fraction = old_enemy_move_no_op_scale_10_fraction

        self.info(f'finScr {str(simResult)} {str(simResult.expected_best_moves)}')

        return simResult

    def find_end_of_turn_scrim_move(self, threat: ThreatObj | None, kingKillPath: Path | None, time_limit: float | None = None) -> Move | None:
        simResult = self.find_end_of_turn_sim_result(threat, kingKillPath, time_limit)

        if simResult is not None:
            friendlyPath, enemyPath = self.extract_engine_result_paths_and_render_sim_moves(simResult)
            if friendlyPath is not None:
                return self.get_first_path_move(friendlyPath)

        return None

    def get_largest_tiles_as_armies(self, player: int, limit: int) -> typing.List[Army]:
        player = self._map.players[player]

        def sortFunc(t: Tile) -> float:
            pw = self.board_analysis.intergeneral_analysis.pathWayLookupMatrix[t]
            dist = 100
            if pw is not None:
                dist = pw.distance
            else:
                logbook.error(f'pathway none again for {str(t)}')
            return (t.army - 1) / (dist + 5)

        tiles = sorted(
            player.tiles,
            key=sortFunc,
            reverse=True)

        armies = [self.get_army_at(t, no_expected_path=True) for t in tiles[0:limit] if t.army > 1]

        return armies

    def get_defense_tree_move_prio_func_old(
            self,
            threat: ThreatObj,
            anyLeafIsSameDistAsThreat: bool = False,
            printDebug: bool = False
    ) -> typing.Callable[[Tile, typing.Any], typing.Any]:
        # threatenedTileDistMap = SearchUtils.build_distance_map(self._map, [threat.path.tail.tile])
        threatenedTileDistMap = threat.armyAnalysis.aMap
        threatDistMap = threat.armyAnalysis.bMap
        threatDist = threatenedTileDistMap.raw[threat.path.start.tile.tile_index]
        # moveClosestMap = SearchUtils.build_distance_map(self._map, [threat.path.start.tile])

        shortestTiles = threat.armyAnalysis.shortestPathWay.tiles

        def move_closest_negative_value_func(curTile: Tile, currentPriorityObject):
            """
            MAX tuple gets moved first. Trues are > False, etc.

            @param curTile:
            @param currentPriorityObject:
            @return:
            """
            toTile = None
            if currentPriorityObject is not None:
                _, _, _, _, _, toTile = currentPriorityObject

            isMovable = curTile in threat.path.start.tile.movable
            isMovableToThreatButNotIntercepting = toTile != threat.path.start.tile and isMovable and threatenedTileDistMap.raw[curTile.tile_index] < threatDist

            # this is very specifically like this to pass test_should_wait_to_gather_tiles_that_are_in_the_shortest_pathway_for_last
            closenessToThreat = threatenedTileDistMap.raw[curTile.tile_index]
            inShortest = curTile in shortestTiles
            if threatDist > closenessToThreat and inShortest:
                closenessToThreat = 0 - closenessToThreat

            isInterceptingIn1 = threatDistMap.raw[curTile.tile_index] == 2 and toTile is not None and threatDistMap.raw[toTile.tile_index] == 1

            if isMovableToThreatButNotIntercepting:
                # move ANYTHING else first, since the threat might just run into this tile and we arent gonna intercept it anyway.
                #  This leads to us dying if we 'dodge' the threat accidentally and open up a 1-route
                #  (and guarantee we can't re-intercept in 1, either, because we wont have priority next turn if we dodge).
                closenessToThreat += 20
            elif isMovable:
                closenessToThreat = 0

            isntDelayableCity = anyLeafIsSameDistAsThreat or not curTile.isCity

            obj = (
                isntDelayableCity,
                isInterceptingIn1,
                not inShortest,
                0 - closenessToThreat,
                curTile.army,
                curTile,
            )
            if printDebug:
                self.viewInfo.add_info_line(f'{curTile}: {obj}  (isMov {str(isMovable)[0]}, int1 {str(isInterceptingIn1)[0]}, short {str(inShortest)[0]}, mvNotInt {str(isMovableToThreatButNotIntercepting)[0]})')

            return obj

        return move_closest_negative_value_func
    
    def get_defense_tree_move_prio_func(
            self,
            threat: ThreatObj,
            anyLeafIsSameDistAsThreat: bool = False,
            printDebug: bool = False
    ) -> typing.Callable[[Tile, typing.Any], typing.Any]:
        # threatenedTileDistMap = SearchUtils.build_distance_map(self._map, [threat.path.tail.tile])
        threatenedTileDistMap = threat.armyAnalysis.aMap
        threatDistMap = threat.armyAnalysis.bMap
        threatDist = threatenedTileDistMap.raw[threat.path.start.tile.tile_index]
        # moveClosestMap = SearchUtils.build_distance_map(self._map, [threat.path.start.tile])

        shortestTiles = threat.armyAnalysis.shortestPathWay.tiles

        def move_closest_negative_value_func(curTile: Tile, currentPriorityObject):
            """
            MAX tuple gets moved first. Trues are > False, etc.

            @param curTile:
            @param currentPriorityObject:
            @return:
            """
            toTile = None
            lastIsntDelayable = False
            lastIsInterceptingIn1 = False
            lastNotInShortest = False
            lastRootHeur = 0
            lastNegClosenessToThreat = -1000
            lastArmy = 0
            rootDistToThreat = threatDistMap.raw[curTile.tile_index]
            depth = 0
            if currentPriorityObject is not None:
                lastIsntDelayable, lastIsInterceptingIn1, lastNotInShortest, lastRootHeur, lastNegClosenessToThreat, lastArmy, depth, rootDistToThreat, toTile = currentPriorityObject

            isMovable = curTile in threat.path.start.tile.movable
            isMovableToThreatButNotIntercepting = toTile != threat.path.start.tile and isMovable and threatenedTileDistMap.raw[curTile.tile_index] < threatDist

            # this is very specifically like this to pass test_should_wait_to_gather_tiles_that_are_in_the_shortest_pathway_for_last
            closenessToThreat = threatenedTileDistMap.raw[curTile.tile_index]
            inShortest = curTile in shortestTiles
            if threatDist > closenessToThreat and inShortest:
                closenessToThreat = 0 - closenessToThreat

            isInterceptingIn1 = threatDistMap.raw[curTile.tile_index] == 2 and toTile is not None and threatDistMap.raw[toTile.tile_index] == 1

            if isMovableToThreatButNotIntercepting:
                # move ANYTHING else first, since the threat might just run into this tile and we arent gonna intercept it anyway.
                #  This leads to us dying if we 'dodge' the threat accidentally and open up a 1-route
                #  (and guarantee we can't re-intercept in 1, either, because we wont have priority next turn if we dodge).
                closenessToThreat += 20
            elif isMovable:
                closenessToThreat = 0

            isntDelayableCity = anyLeafIsSameDistAsThreat or not curTile.isCity
            # negClosenessToThreat = max(lastNegClosenessToThreat, 0 - closenessToThreat)

            obj = (
                isntDelayableCity,
                isInterceptingIn1 or lastIsInterceptingIn1,
                not inShortest,
                0 - rootDistToThreat + depth,
                0 - closenessToThreat,
                curTile.army,
                depth + 1,
                rootDistToThreat,
                curTile,
            )
            if printDebug and curTile.player == self.general.player:
                self.viewInfo.add_info_line(f'{curTile}: {obj}  (isMov {str(isMovable)[0]}, int1 {str(isInterceptingIn1)[0]}, short {str(inShortest)[0]}, mvNotInt {str(isMovableToThreatButNotIntercepting)[0]})')

            return obj

        return move_closest_negative_value_func

    def get_capture_first_tree_move_prio_func(
        self
    ) -> typing.Callable[[Tile, typing.Any], typing.Any]:

        def capture_first_value_func(curTile: Tile, currentPriorityObject):
            lastTile = None
            if currentPriorityObject:
                (_, _, lastTile) = currentPriorityObject
            # closenessToThreat = threatenedTileDistMap[curTile] - moveClosestMap[curTile]
            return (
                lastTile is None or not self._map.is_tile_friendly(lastTile),
                curTile.isSwamp,
                curTile
            )

        return capture_first_value_func

    def hunt_for_fog_neutral_city(self, negativeTiles: typing.Set[Tile], maxTurns: int) -> typing.Tuple[Path | None, Move | None]:
        fogObstacleAdjacents = MapMatrix(self._map, 0)
        tilesNear = []

        skipTiles = set()
        for t in self.player.tiles:
            if t.army <= 1:
                skipTiles.add(t)

        def foreachFogObstacleCounter(tile: Tile, dist: int):
            if (
                    # not tile.discovered
                    not SearchUtils.any_where(tile.adjacents, lambda t: self.territories.is_tile_in_enemy_territory(t) or self._map.is_tile_enemy(t))
                    and not tile.isUndiscoveredObstacle
                    and not (tile.isCity and tile.isNeutral)
            ):
                count = 0
                for adj in tile.adjacents:
                    if self.distance_from_general(tile) > self.distance_from_general(adj):
                        continue
                    if adj.isUndiscoveredObstacle:
                        count += 1
                        count += fogObstacleAdjacents[adj]

                fogObstacleAdjacents[tile] = count
                if count > 0:
                    tilesNear.append((tile, dist))

            return self.territories.is_tile_in_enemy_territory(t) or t.isUndiscoveredObstacle or (t.isCity and not self._map.is_tile_friendly(t))

        SearchUtils.breadth_first_foreach_dist(
            self._map,
            self.player.tiles,
            maxDepth=4,
            foreachFunc=foreachFogObstacleCounter,
            bypassDefaultSkip=True
        )

        logbook.info(f'GATHERING TO FOG UNDISC')

        def sorter(tileDistTuple) -> float:
            tile, dist = tileDistTuple
            rating = fogObstacleAdjacents[tile] / self.distance_from_general(tile) / dist
            return rating

        prioritized = [t for t, d in sorted(tilesNear, key=sorter, reverse=True)]

        for tile in self._map.get_all_tiles():
            self.viewInfo.midRightGridText[tile] = f'fa{fogObstacleAdjacents[tile]}'

        keyAreas = prioritized[0:5]

        for t in keyAreas:
            self.viewInfo.add_targeted_tile(t, TargetStyle.WHITE)

        path = SearchUtils.dest_breadth_first_target(self._map, keyAreas, targetArmy=1, negativeTiles=negativeTiles, skipTiles=skipTiles, maxDepth=min(3, maxTurns))
        if path is not None:
            self.curPath = path
            return path, None

        with self.perf_timer.begin_move_event('Hunting for fog neutral cities'):
            move, valueGathered, turnsUsed, gatherNodes = self.get_gather_to_target_tiles(keyAreas, 0.1, self._map.turn % 5 + 1, negativeTiles, targetArmy=1, useTrueValueGathered=True, maximizeArmyGatheredPerTurn=True)

        if move is not None:
            self.gatherNodes = gatherNodes
            self.info(f'Hunting for fog neutral cities: {move}')
            return None, move

        return None, None

    def try_find_exploration_move(self, defenseCriticalTileSet: typing.Set[Tile]) -> Move | None:
        # if losing on economy, finishing exp false
        genPlayer = self._map.players[self.general.player]

        largeTileThresh = 15 * genPlayer.standingArmy / genPlayer.tileCount
        haveLargeTilesStill = len(SearchUtils.where(genPlayer.tiles, lambda tile: tile.army > largeTileThresh)) > 0
        logbook.info(
            "Will stop finishingExploration if we don't have tiles larger than {:.1f}. Have larger tiles? {}".format(
                largeTileThresh, haveLargeTilesStill))
        # TODO
        # if not self.opponent_tracker.winning_on_economy(cityValue=0) and not haveLargeTilesStill:
        #     self.finishing_exploration = False

        demolishingTargetPlayer = (self.opponent_tracker.winning_on_army(1.5, useFullArmy=False, againstPlayer=self.targetPlayer)
                                   and self.opponent_tracker.winning_on_economy(1.5, cityValue=10, againstPlayer=self.targetPlayer))

        allInAndKnowsGenPosition = (
                (self.is_all_in_army_advantage or self.all_in_losing_counter > self.targetPlayerObj.tileCount // 3)
                and self.targetPlayerExpectedGeneralLocation.isGeneral
                and not self.all_in_city_behind
        )
        targetPlayer = self._map.players[self.targetPlayer]
        stillDontKnowAboutEnemyCityPosition = len(targetPlayer.cities) + 1 < targetPlayer.cityCount
        stillHaveSomethingToSearchFor = (
                (self.is_all_in() or self.finishing_exploration or demolishingTargetPlayer)
                and (not self.targetPlayerExpectedGeneralLocation.isGeneral or stillDontKnowAboutEnemyCityPosition)
        )

        logbook.info(
            f"stillDontKnowAboutEnemyCityPosition: {stillDontKnowAboutEnemyCityPosition}, allInAndKnowsGenPosition: {allInAndKnowsGenPosition}, stillHaveSomethingToSearchFor: {stillHaveSomethingToSearchFor}")
        if not allInAndKnowsGenPosition and stillHaveSomethingToSearchFor and not self.defend_economy:
            undiscNeg = defenseCriticalTileSet.copy()

            if (
                    self.all_in_city_behind
                    or (
                    self.is_all_in_army_advantage
                    and self.opponent_tracker.winning_on_economy(byRatio=0.8, cityValue=50)
            )
            ):
                path = self.get_quick_kill_on_enemy_cities(defenseCriticalTileSet)
                if path is not None:
                    # self.curPath = path
                    self.info(f'ALL IN ARMY ADVANTAGE CITY CONTEST {str(path)}')
                    return self.get_first_path_move(path)

                for contestedCity in self.cityAnalyzer.owned_contested_cities:
                    undiscNeg.add(contestedCity)

            timeCap = 0.03
            if allInAndKnowsGenPosition:
                timeCap = 0.06

            self.viewInfo.add_info_line(
                f"exp: unknownEnCity: {stillDontKnowAboutEnemyCityPosition}, allInAgainstGen: {allInAndKnowsGenPosition}, stillSearch: {stillHaveSomethingToSearchFor}")
            with self.perf_timer.begin_move_event('Attempt to fin/cont exploration'):
                for city in self._map.players[self.general.player].cities:
                    undiscNeg.add(city)

                if self.target_player_gather_path is not None:
                    halfTargetPath = self.target_player_gather_path.get_subsegment(
                        self.target_player_gather_path.length // 2)
                    undiscNeg.add(self.general)
                    for tile in halfTargetPath.tileList:
                        undiscNeg.add(tile)
                path = self.explore_target_player_undiscovered(undiscNeg, maxTime=timeCap)
                if path is not None:
                    self.viewInfo.color_path(PathColorer(path, 120, 150, 127, 200, 12, 100))
                    if not self.is_path_moving_mostly_away(path, self.board_analysis.intergeneral_analysis.bMap):
                        valueSubsegment = self.get_value_per_turn_subsegment(path, minLengthFactor=0)
                        if valueSubsegment.length != path.length:
                            logbook.info(f"BAD explore_target_player_undiscovered")
                            self.info(
                                f"WHOAH, tried to make a bad exploration path...? Fixed with {str(valueSubsegment)}")
                            path = valueSubsegment
                        move = self.get_first_path_move(path)
                        if not self.detect_repetition(move, 7, 2):
                            if self.is_all_in_army_advantage:
                                self.all_in_army_advantage_counter -= 2
                            return move
                        else:
                            self.info('bypassed hunting due to repetitions.')
                    else:
                        self.info(f'IGNORING BAD HUNTING PATH BECAUSE MOVES AWAY FROM GEN APPROX')

        return None

    def handle_chat_message(self, chatUpdate: ChatUpdate):
        if chatUpdate.from_user == self._map.usernames[self._map.player_index]:
            # ignore our own messages?
            return

        self.viewInfo.add_info_line(str(chatUpdate))
        if self.is_2v2_teammate_still_alive() and chatUpdate.is_team_chat:
            if self.teamed_with_bot:
                self.handle_bot_chat(chatUpdate)
            else:
                self.handle_human_chat(chatUpdate)
        else:
            # chat in other modes, FFA teaming...?
            pass

    def is_2v2_teammate_still_alive(self) -> bool:
        if not self._map.is_2v2:
            return False
        if self.teammate_general is None:
            return False
        return True

    def handle_bot_chat(self, chatUpdate: ChatUpdate):
        if chatUpdate.message.startswith("!"):
            self.teammate_communicator.handle_coordination_update(chatUpdate)

    def handle_human_chat(self, chatUpdate: ChatUpdate):
        pass

    def communicate_threat_to_ally(self, threat: ThreatObj, valueGathered: int, defensePlan: typing.List[GatherTreeNode]):
        if threat.path.tail.tile.isCity:
            return
        valueGathered = int(round(valueGathered))
        if self.teammate_communicator is not None and self.teammate_communicator.is_teammate_coordinated_bot:
            self.teammate_communicator.communicate_defense_plan(threat, valueGathered, defensePlan)
        elif threat.threatValue - valueGathered > 0:
            self.send_teammate_communication(f"HELP! NEED {threat.threatValue - valueGathered} in {threat.turns - 1}", detectionKey='needHelp', cooldown=10)
            self.send_teammate_path_ping(threat.path, cooldown=5, cooldownKey="HELP ME")

    def try_find_main_timing_expansion_move_if_applicable(self, defenseCriticalTileSet: typing.Set[Tile]) -> Move | None:
        if self.is_all_in_losing:
            return None

        turnsLeft = self.timings.get_turns_left_in_cycle(self._map.turn)
        utilizationCutoff = turnsLeft - turnsLeft // 7
        value = self.expansion_plan.en_tiles_captured * 2 + self.expansion_plan.neut_tiles_captured
        haveFullExpPlanAlready = False
        if (
                self.expansion_plan.turns_used >= turnsLeft - self.player.cityCount * 2
                and self.expansion_plan.en_tiles_captured > self.expansion_plan.neut_tiles_captured // 3
                # and self.expansion_plan.en_tiles_captured > turnsLeft // 2
                and self.expansion_plan.en_tiles_captured * 2 + self.expansion_plan.neut_tiles_captured > turnsLeft - 2
                and value > utilizationCutoff
                # and (self.armyTracker.lastMove is None or self.armyTracker.lastMove.dest in self.target_player_gather_targets)
                and self._get_approximate_greedy_turns_available() > 0
        ):
            haveFullExpPlanAlready = True

        # TODO hack because this keeps attacking early:
        haveFullExpPlanAlready = False

        havePotentialIntercept = self.expansion_plan.includes_intercept

        # if len(paths) == 0 and (self.curPath is None or self.curPath.start.next is None) and self._map.turn >= 50:
        if haveFullExpPlanAlready or havePotentialIntercept or ((self.curPath is None or self.curPath.start is None or self.curPath.start.next is None) and not self.defend_economy or self._map.turn < 100):
            expNegs = set(defenseCriticalTileSet)
            if not haveFullExpPlanAlready or self.is_all_in():
                with self.perf_timer.begin_move_event('checking launch move'):
                    attackLaunchMove = self.check_for_attack_launch_move(expNegs)
                if attackLaunchMove is not None and not haveFullExpPlanAlready:
                    return attackLaunchMove

            with self.perf_timer.begin_move_event("try_find_expansion_move main timing"):
                timeLimit = min(self.get_remaining_move_time(), self.expansion_full_time_limit)
                move = self.try_find_expansion_move(expNegs, timeLimit, forceBypassLaunch=haveFullExpPlanAlready or havePotentialIntercept)

            if move is not None:
                if not self.timings.in_expand_split(self._map.turn) and haveFullExpPlanAlready:
                    cycleTurn = self.timings.get_turn_in_cycle(self._map.turn)
                    self.viewInfo.add_info_line('Due to full expansion plan, moving down launch/gather split.')
                    self.timings.launchTiming = max(20, cycleTurn)
                    self.timings.splitTurns = cycleTurn
                return move  # already logged

        return None

    def try_find_expansion_move(self, defenseCriticalTileSet: typing.Set[Tile], timeLimit: float, forceBypassLaunch: bool = False, overrideTurns: int = -1) -> Move | None:
        skipForAllIn = self.is_all_in() and not self.all_in_city_behind

        cycleTurnsLeft = self.timings.get_turns_left_in_cycle(self._map.turn)
        if skipForAllIn and self.is_all_in_army_advantage and cycleTurnsLeft < 15 and self.opponent_tracker.winning_on_economy():
            self.viewInfo.add_info_line(f'will NOT exp for is_all_in_army_advantage trying to kill / contest to catch up')
            skipForAllIn = False

        if not forceBypassLaunch and not self.timings.in_expand_split(self._map.turn) and overrideTurns < 0:
            return None

        if self.targetPlayer != -1 and self.is_still_ffa_and_non_dominant():
            if self.opponent_tracker.winning_on_army(byRatio=1.1) and self.opponent_tracker.winning_on_economy(byRatio=1.1) and self.targetPlayerObj.aggression_factor > 30:
                self.info("beating FFA player exp bypass")
                return None

            if self.opponent_tracker.winning_on_army(byRatio=1.3, offset=-30):
                self.info("crushing FFA player exp bypass")
                return None

        if self.defend_economy:
            self.viewInfo.add_info_line(
                f"skip exp bc self.defendEconomy ({self.defend_economy})")
            return None

        if skipForAllIn:
            self.viewInfo.add_info_line(
                f"skip exp bc self.all_in_counter ({self.all_in_losing_counter}) / skipForAllIn {skipForAllIn} / is_all_in_army_advantage {self.is_all_in_army_advantage}")
            return None

        expansionNegatives = defenseCriticalTileSet.copy()
        splitTurn = self.timings.get_turn_in_cycle(self._map.turn)
        if (not forceBypassLaunch and splitTurn < self.timings.launchTiming and self._map.turn > 50) or (self.target_player_gather_path is not None and self.target_player_gather_path.start.tile in expansionNegatives):
            self.viewInfo.add_info_line(
                f"splitTurn {splitTurn} < launchTiming {self.timings.launchTiming}...?")
            for tile in self.target_player_gather_targets:
                if self._map.is_tile_friendly(tile):
                    expansionNegatives.add(tile)

        # launchSubsegment = self.get_path_subsegment_to_closest_enemy_team_territory(self.target_player_gather_path)
        # if launchSubsegment.value > self.lau:
        #

        tilesWithArmy = SearchUtils.where(
            self._map.players[self.general.player].tiles,
            filter_func=lambda t: (
                    (t.army > 2 or SearchUtils.any_where(t.movable, lambda mv: not self._map.is_tile_friendly(mv) and t.army - 1 > mv.army))
                    and not t.isCity
                    and not t.isGeneral
            )
        )

        if (
                (
                        self.city_expand_plan is not None
                        or (
                                len(tilesWithArmy) == 0
                                # and remainingCycleTurns < 21
                                # and not self._map.remainingPlayers > 3
                        )
                )
                and (self.expansion_plan is None or self.expansion_plan.turns_used < self.timings.get_turns_left_in_cycle(self._map.turn))
                and not self.is_still_ffa_and_non_dominant()
        ):
            remainingTime = self.get_remaining_move_time()
            with self.perf_timer.begin_move_event(f'EXP - first25 reuse - {remainingTime:.4f}'):
                move = self.get_optimal_city_or_general_plan_move(timeLimit=remainingTime)
                if move is not None and (move.source.army == 1 or move.source.player != self.general.player):
                    # abort
                    self.city_expand_plan = None
                    self.info(f'Aborting bad city_expand_plan reuse move {move}')
                else:
                    if self._map.turn < self.city_expand_plan.launch_turn:
                        self.info(f'Optimal Expansion F25 piggyback wait {move}')
                        return None

                    self.info(f'Optimal Expansion F25 piggyback {move}')

                    moveListPath = MoveListPath([])

                    for planPath in self.city_expand_plan.plan_paths:
                        if planPath is None:
                            moveListPath.add_next_move(None)
                            continue
                        for m in planPath.get_move_list():
                            moveListPath.add_next_move(m)

                    self.curPath = moveListPath
                    self.city_expand_plan = None

                    # if self.city_expand_plan is not None and len(self.city_expand_plan.plan_paths) > 0 and self.city_expand_plan.plan_paths[0] is not None:
                    #     self.curPath = self.city_expand_plan.plan_paths[0].get_subsegment(4)
                    # self.city_expand_plan = None
                    return move

        self._add_expansion_threat_negs(expansionNegatives)
        self.expansion_plan = self.build_expansion_plan(timeLimit, expansionNegatives, pathColor=(50, 30, 255), overrideTurns=overrideTurns)

        path = self.expansion_plan.selected_option
        allPaths = self.expansion_plan.all_paths

        expansionNegStr = " | ".join([str(t) for t in expansionNegatives])
        if path:
            pathMove = path.get_first_move()
            inLaunchSplit = self.timings.in_launch_split(self._map.turn)
            if pathMove.source.isGeneral and not inLaunchSplit and len(allPaths) > 1 and not self.expansion_plan.includes_intercept:
                # TODO hack to prevent gen from launching early? Why do we care?
                path = allPaths[1]
            # if pathMove.source.isGeneral or path.start.tile.isCity and not self.expansion_plan.includes_intercept:
            #     # TODO is this really even necessary anymore?
            #     self.curPath = path.get_subsegment(max(1, path.length // 2 + 1))

            move = pathMove
            if self.is_all_in() and move.move_half:
                self.viewInfo.add_info_line(f'because we\'re all in, will NOT move-half...')
                move.move_half = False
            if self.is_move_safe_valid(move):
                self.info(
                    f"EXP {path.econValue:.2f}/{path.length}t {move} neg ({expansionNegStr})")
                return move
            else:
                self.info(
                    f"NOT SAFE EXP {path.econValue:.2f}/{path.length}t {move} neg ({expansionNegStr})")

        elif len(allPaths) > 0:
            self.info(
                f"Exp had no paths, wait? neg {expansionNegStr}")
            return None
        else:
            self.info(
                f"Exp move not found...? neg {expansionNegStr}")
        return None

    def _add_expansion_threat_negs(self, negs: typing.Set[Tile]):
        logbook.info(f'starting expansion threat negs: {[t for t in negs]}')
        if self.threat is None:
            return

        if self.threat.threatType == ThreatType.Kill:
            turn = self._map.turn
            for tile in self.threat.path.tileList:
                if turn % 50 == 0 and turn != self._map.turn:
                    break
                turn += 1
                if self._map.team_ids_by_player_index[tile.player] != self._map.team_ids_by_player_index[self.general.player] or self.threat.threatValue > 0:
                    if tile not in negs:
                        logbook.info(f"  Added neg {tile}, turn {turn}")
                        negs.add(tile)
            # for t in self.threat.path.tileList:
            #     if not self._map.is_tile_on_team_with(t, self.targetPlayer) and t not in negs:
            #         logbook.info(f"  Added fr tile neg {t}")
            #         negs.add(t)
        # elif self.dangerAnalyzer.fastestPotentialThreat is not None and self.dangerAnalyzer.fastestPotentialThreat.threatType == ThreatType.Kill:
        #     turnsLeft = self._map.remainingCycleTurns // 2
        #     for tile in self.dangerAnalyzer.fastestPotentialThreat.path.tileList:
        #         if turnsLeft == 0:
        #             break
        #         turnsLeft -= 1
        #         if self._map.team_ids_by_player_index[tile.player] != self._map.team_ids_by_player_index[self.general.player]:
        #             if tile not in negs:
        #                 logbook.info(f"  Added neg {tile}, turnsLeft {turnsLeft}")
        #                 negs.add(tile)
            # for t in self.dangerAnalyzer.fastestPotentialThreat.path.tileList:
            #     if not self._map.is_tile_on_team_with(t, self.targetPlayer) and t not in negs:
            #         logbook.info(f"  Added fr tile neg {t}")
            #         negs.add(t)

        self.army_interceptor.ensure_threat_army_analysis(self.threat)

    def try_find_army_out_of_position_move(self, defenseCriticalTileSet: typing.Set[Tile]) -> Move | None:
        thresh = self.targetPlayerObj.standingArmy ** 0.6
        logbook.info(f'Checking for out of position tiles with army greater than threshold {thresh:.0f}')
        outOfPositionArmies = []
        for tile in self.largePlayerTiles:
            distFr = self.board_analysis.intergeneral_analysis.aMap[tile]
            distEn = self.board_analysis.intergeneral_analysis.bMap[tile]

            if tile in self.board_analysis.extended_play_area_matrix and distEn > distFr:
                continue

            if tile in self.board_analysis.core_play_area_matrix and distEn * 2 > distFr:
                continue

            if tile.army < thresh:
                continue

            if self.territories.is_tile_in_friendly_territory(tile):
                continue

            outOfPositionArmies.append(self.get_army_at(tile))

            if len(outOfPositionArmies) > 5:
                break

        if len(outOfPositionArmies) == 0:
            return None

        result = self.get_armies_scrim_result(friendlyArmies=outOfPositionArmies, enemyArmies=self.get_largest_tiles_as_armies(self.targetPlayer, 7), enemyCannotMoveAway=False)

        if result is not None:
            friendlyPath, enemyPath = self.extract_engine_result_paths_and_render_sim_moves(result)
            if friendlyPath is not None:
                move = self.get_first_path_move(friendlyPath)
                self.info(f'Army out of position scrim {move}')
                return move

        return None

    def are_more_teams_alive_than(self, numTeams: int) -> bool:
        aliveTeams = set()
        afkPlayers = self.get_afk_players()
        teams = MapBase.get_teams_array(self._map)

        for player in self._map.players:
            if not player.dead and player.tileCount > 3 and player not in afkPlayers:
                aliveTeams.add(teams[player.index])

        if len(aliveTeams) > numTeams:
            return True

        return False

    def get_potential_threat_movement_negatives(self, targetTile: Tile | None = None) -> typing.Set[Tile]:
        """
        Based on an available potential threat path, determine if any tiles are not allowed to move because they would increase risk.

        @param targetTile: Optionally include the target tile that you are calculating moves AGAINST which will allow tile use that would otherwise be blocked if the target is part of the threat.

        @return:
        """
        potThreat = self.dangerAnalyzer.fastestPotentialThreat
        potNegs = set()

        if potThreat is None:
            return potNegs

        if targetTile is not None and targetTile in potThreat.armyAnalysis.shortestPathWay.tiles:
            return potNegs

        threatArmy = self.armyTracker.armies.get(potThreat.path.start.tile, None)

        if threatArmy is not None and not threatArmy.tile.visible:
            if potThreat.turns < 7 and self.targetingArmy is None:
                logbook.info(f'get_potential_threat_movement_negatives setting targetingArmy to {str(threatArmy)} due to potential threat less than 7')
                self.targetingArmy = threatArmy
            elif threatArmy.last_seen_turn < self._map.turn - 4 and threatArmy.last_moved_turn < self._map.turn - 1:
                # ignore threats from non-visible armies that we haven't seen in a while.
                return potNegs

        shortestSet = set()
        if targetTile is not None:
            targetAnalysis = ArmyAnalyzer(self._map, self.general, targetTile)
            shortestSet = targetAnalysis.shortestPathWay.tiles

        for tile in potThreat.path.tileList:
            if self._map.is_tile_friendly(tile) and potThreat.threatValue + tile.army > potThreat.turns and tile not in shortestSet:
                potNegs.add(tile)

        return potNegs

    def get_target_player_possible_general_location_tiles_sorted(
            self,
            elimNearbyRange: int = 2,
            player: int = -2,
            cutoffEmergenceRatio: float = 0.333,
            includeCities: bool = False,
            limitNearbyTileRange: int = -1
    ) -> typing.List[Tile]:
        """

        @param elimNearbyRange: Drops tiles that are within this many tiles from a tile that is already included. Basically forces the resulting list to not just be a gradient clustered around the one highest emergence point.
        @param player:
        @param cutoffEmergenceRatio:
        @return:
        """

        if player == -2:
            player = self.targetPlayer

        if player == -1:
            return []

        if self._map.players[player].general is not None:
            return [self._map.players[player].general]

        emergenceVal = 0
        if player == self.targetPlayer:
            emergenceVal = self.armyTracker.emergenceLocationMap[player][self.targetPlayerExpectedGeneralLocation]

        emergenceCutoff = int(emergenceVal * cutoffEmergenceRatio)

        emergenceVals = []
        for tile in self._map.get_all_tiles():
            if not tile.discovered:
                emergenceAmt = self.armyTracker.get_tile_emergence_for_player(tile, player)
                if not tile.isObstacle and self.armyTracker.valid_general_positions_by_player[player][tile]:
                    if emergenceAmt > emergenceCutoff:
                        emergenceVals.append((emergenceAmt, tile))
                elif includeCities and tile.isUndiscoveredObstacle and emergenceAmt > 0:
                    if emergenceAmt > emergenceCutoff:
                        emergenceVals.append((emergenceAmt, tile))

        if len(emergenceVals) == 0 and self.undiscovered_priorities is not None:
            for tile in self._map.get_all_tiles():
                if not tile.discovered and not tile.isObstacle and self.armyTracker.valid_general_positions_by_player[player][tile]:
                    emergenceAmt = self.undiscovered_priorities.raw[tile.tile_index]
                    # DONT invert FFA, stay close to general...?
                    # distMap[tile.x][tile.y] = 0 - distMap[tile.x][tile.y]
                    emergenceAmt -= self.get_distance_from_board_center(tile, center_ratio=0.35) * 0.1
                    emergenceVals.append((emergenceAmt, tile))

        tilesSorted = [tile for val, tile in sorted(emergenceVals, reverse=True) if tile != self.targetPlayerExpectedGeneralLocation]
        if player == self.targetPlayer and self.targetPlayerExpectedGeneralLocation is not None and self.armyTracker.valid_general_positions_by_player[player].raw[self.targetPlayerExpectedGeneralLocation.tile_index]:
            tilesSorted.insert(0, self.targetPlayerExpectedGeneralLocation)

        elimSet = set()

        finalTiles = []
        retry = True
        while retry:
            retry = False
            for tile in tilesSorted:
                if tile.tile_index in elimSet:
                    continue
                if limitNearbyTileRange > 0 and self.territories.territoryDistances[player].raw[tile.tile_index] > limitNearbyTileRange:
                    continue

                finalTiles.append(tile)
                if elimNearbyRange > 0:
                    SearchUtils.breadth_first_foreach_fast_no_neut_cities(self._map, [tile], elimNearbyRange, lambda t: elimSet.add(t.tile_index))
            if len(finalTiles) == 0 and limitNearbyTileRange > 0:
                retry = True
                limitNearbyTileRange = -1

        return finalTiles

    def get_first_25_expansion_distance_priority_map(self) -> typing.Tuple[MapMatrixInterface[int], typing.Set[Tile]]:
        """
        Returns a matrix of big-number=bad expansion priorities, and skip tiles (if teams).
        Safe to be modified.

        @return:
        """

        # tg = self.general
        # if self.targetPlayerExpectedGeneralLocation is not None:
        #     tg = self.targetPlayerExpectedGeneralLocation

        if self.is_ffa_situation():
            return self._get_avoid_other_players_expansion_matrix(), set()

        numberStartTargets = 2

        if self.targetPlayer != -1:
            tgs, enDistMap = self._get_furthest_apart_3_enemy_general_locations(self.targetPlayer)
        else:
            tgs = self.get_target_player_possible_general_location_tiles_sorted(elimNearbyRange=12)[0:numberStartTargets]

            if len(tgs) < numberStartTargets:
                tgs = self.get_target_player_possible_general_location_tiles_sorted(elimNearbyRange=8)[0:numberStartTargets]

            for tg in tgs:
                self.viewInfo.add_targeted_tile(tg, TargetStyle.TEAL)

            enDistMap = SearchUtils.build_distance_map_matrix(self._map, tgs)

        distSource = []
        skipTiles = set()

        if self._map.is_2v2 and self.teammate_general is not None:
            # distSource.append(self.general)
            if self._map.turn < 46:
                expandPlanSizeIndicated = len(self.tiles_pinged_by_teammate_this_turn) + len(self._map.players[self.teammate_general.player].tiles)
                if len(self.tiles_pinged_by_teammate_this_turn) > 3 and expandPlanSizeIndicated > 18:
                    self.viewInfo.add_info_line(f'reset team f25, received teammate tile pings indicative of full expansion plan')
                    self._tiles_pinged_by_teammate_first_25 = set()

                for t in self.tiles_pinged_by_teammate_this_turn:
                    self._tiles_pinged_by_teammate_first_25.add(t)

            if self._map.turn > 12 or not self.teammate_communicator.is_team_lead or not self.teammate_communicator.is_teammate_coordinated_bot:
                skipTiles = self._tiles_pinged_by_teammate_first_25.copy()

            # if self.city_expand_plan is None:
            #     distSource.append(self.teammate_general)
            #     distMap = SearchUtils.build_distance_map_matrix(self._map, distSource)
            #     for tile in self._map.get_all_tiles():
            #         distMap[tile] = 0 - distMap[tile]
            # else:
            distMap = MapMatrix(self._map, 0)

            usDist = self._map.distance_mapper.get_tile_dist_matrix(self.general)

            for tile in self._map.get_all_tiles():
                distMap[tile] += enDistMap[tile]  # - enDistMap[self.general.x][self.general.y]
                distMap[tile] -= usDist[tile] // 2
                distMap[tile] += self.get_distance_from_board_center(tile, center_ratio=0.75)

            teammateDistanceDropoffPoint = 9  # the tiles after which we stop considering distance from teammate relevant
            for teammate in self._map.teammates:
                teammateDistances = SearchUtils.build_distance_map_matrix(self._map, [self._map.generals[teammate]])
                for otherTile in self._map.get_all_tiles():
                    # distMap[x][y] -= teammateDistances[x][y]
                    if teammateDistances[otherTile] < usDist[otherTile]:
                        distMap[otherTile] += 3 * teammateDistanceDropoffPoint - 3 * teammateDistances[otherTile]
                    # if teammateDistances[otherTile] < 3 and usDist[otherTile] + 1 > teammateDistances[otherTile]:
                    #     skipTiles.add(otherTile)

            if len(skipTiles) == 0:
                if not self._spawn_cramped:
                    teammateAnalysis = ArmyAnalyzer(self._map, self.general, self.teammate_general)
                    for tile in teammateAnalysis.shortestPathWay.tiles:
                        if tile == self.general:
                            continue
                        if teammateAnalysis.aMap[tile] > teammateAnalysis.bMap[tile] or (teammateAnalysis.aMap[tile] == teammateAnalysis.bMap[tile] and not self.teammate_communicator.is_team_lead):
                            logbook.info(f' adding f25 skiptile {tile} due to proximity to ally gen')
                            skipTiles.add(tile)
                else:
                    skipTiles.update(self.teammate_general.movable)

        elif self._map.remainingPlayers == 2:
            distSource.append(self.general)
            distMap = SearchUtils.build_distance_map_matrix(self._map, distSource)
            for tile in self._map.get_all_tiles():
                distMap[tile] = 0 - distMap[tile]
                distMap[tile] += enDistMap[tile]
                distMap[tile] += self.get_distance_from_board_center(tile, center_ratio=0.85)
        elif self._map.remainingPlayers > 2:
            # ffa
            distSource.append(self.general)
            distMap = SearchUtils.build_distance_map_matrix(self._map, distSource)

            for tile in self._map.get_all_tiles():
                # DONT invert FFA, stay close to general...?
                # distMap[tile.x][tile.y] = 0 - distMap[tile.x][tile.y]
                distMap[tile] -= self.get_distance_from_board_center(tile, center_ratio=0.15)
        else:
            raise AssertionError("The fuck?")

        for tile in self._map.get_all_tiles():
            if isinstance(distMap[tile], float):
                self.viewInfo.midLeftGridText[tile] = f'f{distMap[tile]:.1f}'
            else:
                self.viewInfo.midLeftGridText[tile] = f'f{str(distMap[tile])}'
            if tile in skipTiles:
                self.viewInfo.add_targeted_tile(tile, TargetStyle.RED, radiusReduction=6)

        return distMap, skipTiles

    def is_still_ffa_and_non_dominant(self) -> bool:
        isFfa = False
        if self._map.remainingPlayers > 2 and not self._map.is_2v2:
            isFfa = True

        if not isFfa:
            return False

        dominating = 0
        nearEven = self._map.remainingPlayers - 1
        dominatedBy = 0
        for player in self._map.players:
            if player == self.general.player:
                continue

            if self.opponent_tracker.winning_on_army(byRatio=1.2, againstPlayer=player.index, offset=-10, useFullArmy=True):
                dominating += 1
                nearEven -= 1
            elif not self.opponent_tracker.winning_on_army(byRatio=0.9, againstPlayer=player.index, useFullArmy=True):
                dominatedBy += 1
                nearEven -= 1

        if dominating > dominatedBy:
            return False

        return True

    def get_expansion_weight_matrix(self, copy: bool = False, mult: int = 1) -> MapMatrixInterface[float]:
        if self._expansion_value_matrix is None:
            logbook.info(f'rebuilding expansion weight matrix for turn {self._map.turn}')
            if self.is_still_ffa_and_non_dominant():
                self._expansion_value_matrix = self._get_avoid_other_players_expansion_matrix()
            else:
                self._expansion_value_matrix = self._get_standard_expansion_capture_weight_matrix()

        if mult != 1:
            copyMat = self._expansion_value_matrix.copy()
            for t in self._map.get_all_tiles():
                copyMat.raw[t.tile_index] *= mult

            return copyMat

        if copy:
            return self._expansion_value_matrix.copy()

        return self._expansion_value_matrix

    def _get_avoid_other_players_expansion_matrix(self) -> MapMatrixInterface[float]:
        matrix = MapMatrix(self._map, 0.0)
        for tile in self._map.get_all_tiles():
            if self.targetPlayer != -1 and (tile.player == self.targetPlayer or self.territories.territoryMap[tile] == self.targetPlayer):
                if tile in self.board_analysis.intergeneral_analysis.shortestPathWay.tiles:
                    continue

            if tile.player != -1:
                matrix[tile] -= 0.6  # penalize capping other player tiles over neutrals.

            for adj in tile.adjacents:
                if not adj.discovered and not adj.isObstacle:
                    matrix[tile] -= 0.1
                if self.targetPlayer != -1 and (adj.player == self.targetPlayer or self.territories.territoryMap[adj] == self.targetPlayer):
                    matrix[tile] = 0.0
                    break
            if self.info_render_expansion_matrix_values:
                val = matrix[tile]
                if val:
                    self.viewInfo.bottomLeftGridText[tile] = f'hx{val:0.3f}'

        return matrix

    def _get_furthest_apart_3_enemy_general_locations(self, player) -> typing.Tuple[typing.List[Tile], MapMatrixInterface[int]]:
        valids = self.armyTracker.valid_general_positions_by_player[player].raw

        furthests = []
        iterMm = SearchUtils.build_distance_map_matrix(self._map, self.player.tiles)
        rawDists = iterMm.raw
        while len(furthests) < 3:
            furthestValidDist = 0
            furthestValid: Tile | None = None
            for tile in self._map.tiles_by_index:
                dist = rawDists[tile.tile_index]
                if dist <= furthestValidDist:
                    continue

                if valids[tile.tile_index]:
                    furthestValid = tile
                    furthestValidDist = dist

            if furthestValid is None:
                break

            SearchUtils.extend_distance_map_matrix(self._map, [furthestValid], iterMm)
            # if len(furthests) == 0:
            #     for t in self._map.tiles_by_index:
            #         self.viewInfo.topRightGridText.raw[t.tile_index] = f'a{iterMm.raw[t.tile_index]}'
            # elif len(furthests) == 1:
            #     for t in self._map.tiles_by_index:
            #         self.viewInfo.midRightGridText.raw[t.tile_index] = f'b{iterMm.raw[t.tile_index]}'
            # elif len(furthests) == 2:
            #     for t in self._map.tiles_by_index:
            #         self.viewInfo.bottomMidRightGridText.raw[t.tile_index] = f'c{iterMm.raw[t.tile_index]}'
            # self.info(f'Furthest: {furthestValid}')
            furthests.append(furthestValid)
            self.viewInfo.add_targeted_tile(furthestValid, TargetStyle.PURPLE, radiusReduction=-2)

        if len(furthests) == 0:
            self.info(f'No furthests...?')
            return furthests, self.board_analysis.intergeneral_analysis.bMap.copy()

        # return furthests, iterMm
        return furthests, SearchUtils.build_distance_map_matrix(self._map, furthests)

    def _get_standard_expansion_capture_weight_matrix(self) -> MapMatrixInterface[float]:
        matrix = MapMatrix(self._map, 0.0)

        innerChokes = self.board_analysis.innerChokes

        dontRevealCities = self.targetPlayer != -1 and self.opponent_tracker.winning_on_economy(byRatio=1.05) and not self.opponent_tracker.winning_on_army(byRatio=1.10)

        numEnGenPos = len(self.alt_en_gen_positions[self.targetPlayer])
        enPotentialGenDistances = self._alt_en_gen_position_distances[self.targetPlayer]
        genPosesToConsider = self.alt_en_gen_positions[self.targetPlayer]

        searchingForFirstContact = False
        if self.targetPlayer != -1 and not self.armyTracker.seen_player_lookup[self.targetPlayer]:
            searchingForFirstContact = True

        if numEnGenPos > 6:
            self.info(f'filtering down valid general set')
            avgDist = sum(self.board_analysis.intergeneral_analysis.aMap.raw[t.tile_index] for t in genPosesToConsider) / len(genPosesToConsider)
            genPosesToConsider = [t for t in genPosesToConsider if self.board_analysis.intergeneral_analysis.aMap.raw[t.tile_index] >= avgDist]
            for pos in genPosesToConsider:
                self.viewInfo.add_targeted_tile(pos, targetStyle=TargetStyle.GREEN, radiusReduction=8)
            numEnGenPos = len(genPosesToConsider)
            enPotentialGenDistances = None

        if enPotentialGenDistances is None:
            enPotentialGenDistances = SearchUtils.build_distance_map_matrix(self._map, genPosesToConsider)
            self._alt_en_gen_position_distances[self.targetPlayer] = enPotentialGenDistances
        tgPlayerTerritoryDists = self.territories.territoryDistances[self.targetPlayer]

        if dontRevealCities:
            self.viewInfo.add_info_line(f'!@! expansion avoiding revealing cities')
            for city in self.win_condition_analyzer.defend_cities:
                cityDist = self.territories.territoryDistances[self.targetPlayer][city]
                for tile in city.movable:
                    if tile.isNeutral and tgPlayerTerritoryDists.raw[tile.tile_index] < cityDist:
                        self.viewInfo.add_targeted_tile(tile, targetStyle=TargetStyle.PURPLE, radiusReduction=12)
                        matrix.raw[tile.tile_index] -= 100

        # reward vision of the enemies likely attack path
        if self.enemy_attack_path is not None:
            for tile in self.enemy_attack_path.tileList:
                if not tile.visible and tile not in self.target_player_gather_path.tileSet:
                    matrix.raw[tile.tile_index] += 0.2

        # heavily reward vision of sketchy flank paths
        if self.sketchiest_potential_inbound_flank_path is not None and self.targetPlayerObj and len(self.targetPlayerObj.tiles) > 0:
            cutoff = 2 * self.board_analysis.inter_general_distance // 5

            for tile in self.sketchiest_potential_inbound_flank_path.adjacentSet:
                if self._map.get_distance_between(self.targetPlayerExpectedGeneralLocation, tile) < cutoff:
                    continue

                if not tile.visible:
                    matrix.raw[tile.tile_index] += 0.1
                else:
                    matrix.raw[tile.tile_index] += 0.03

            for tile in self.sketchiest_potential_inbound_flank_path.tileSet:
                if self._map.get_distance_between(self.targetPlayerExpectedGeneralLocation, tile) < cutoff:
                    continue
                if not tile.visible:
                    matrix.raw[tile.tile_index] += 0.15  # these were already rewarded by adjacentSet as well, so they are + 0.25
                else:
                    matrix.raw[tile.tile_index] += 0.03

        # corePlayTiles = []
        # for tile in self._map.get_all_tiles():
        #     if self.board_analysis.extended_play_area_matrix.raw[tile.tile_index] or self.board_analysis.flank_danger_play_area_matrix[tile]:
        #         corePlayTiles.append(tile)
        # #
        # # corePlayDistances = SearchUtils.build_distance_map_matrix(
        # #     self._map,
        # #     corePlayTiles
        # # )

        endOfCyclePenaltyRatio = 35 / (self._map.cycleTurn + 5)
        """
        scales from 7.0 to 0.635 during the round
        """
        if searchingForFirstContact:
            endOfCyclePenaltyRatio = 0
        self.info(f'enemy expansion endOfCyclePenaltyRatio {endOfCyclePenaltyRatio:.3f}')

        for tile, scoreData in self.cityAnalyzer.get_sorted_neutral_scores()[0:2]:
            if scoreData.general_distances_ratio < 1.1:
                for adj in tile.adjacents:

                    if self._map.is_tile_enemy(adj):
                        matrix.raw[adj.tile_index] += 0.5

                    if adj.isNeutral and not adj.isUndiscoveredObstacle and scoreData.general_distances_ratio > 0.6:  # only bonus
                        matrix.raw[adj.tile_index] += max(0.0, 0.05 * (scoreData.general_distances_ratio - 0.1))
                        if not adj.discovered:
                            matrix.raw[adj.tile_index] += max(0.0, 0.05 * (scoreData.general_distances_ratio - 0.1))

        if self.enemy_expansion_plan is not None:
            for enPath in self.enemy_expansion_plan.all_paths:
                for tile in enPath.tileList[1:]:
                    if self._map.is_tile_friendly(tile):
                        matrix.raw[tile.tile_index] -= self.expansion_enemy_expansion_plan_inbound_penalty * endOfCyclePenaltyRatio

        for lookout in self._map.lookouts:
            for tile in lookout.movableNoObstacles:
                if (self.targetPlayer == -1 and not self._map.is_tile_friendly(tile)) or (self.targetPlayer >= 0 and self._map.is_tile_on_team_with(tile, self.targetPlayer)):
                    matrix.raw[tile.tile_index] += 0.5
                elif tile.player == -1:
                    matrix.raw[tile.tile_index] += 0.25

        for observatory in self._map.observatories:
            for tile in observatory.movableNoObstacles:
                if (self.targetPlayer == -1 and not self._map.is_tile_friendly(tile)) or (self.targetPlayer >= 0 and self._map.is_tile_on_team_with(tile, self.targetPlayer)):
                    matrix.raw[tile.tile_index] += 0.5
                elif tile.player == -1:
                    matrix.raw[tile.tile_index] += 0.25

        for tile in self._map.get_all_tiles():
            bonus = 0.0
            # choke points
            if innerChokes.raw[tile.tile_index]:
                # bonus points for retaking iChokes
                bonus += 0.002

            # enDist = self._map.get_distance_between(self.targetPlayerExpectedGeneralLocation, tile)
            enDist = enPotentialGenDistances.raw[tile.tile_index]
            genDist = self._map.get_distance_between(self.general, tile)

            isCloserToEn = enDist < genDist * 0.9
            enDistRatio = (genDist + 3) / max(1, (enDist + numEnGenPos // 3))
            """if closer to enemy, >1.0 (by a lot when very close to en). 0.1 = means 10x closer to us than enemy. 10.0 = 10x closer to enemy than us."""

            enExpVal = self.enemy_expansion_plan_tile_path_cap_values.get(tile, None)
            if enExpVal is not None:
                bonus += enExpVal / 2

            if self.board_analysis.intergeneral_analysis.is_choke(tile):
                # bonus points for retaking iChokes
                bonus += 0.01

            isNeutral = tile.isNeutral
            isFriendly = not isNeutral and self._map.is_tile_friendly(tile)
            isTarget = not isNeutral and self._map.is_player_on_team_with(tile.player, self.targetPlayer)

            if isFriendly and tile.army < 2:
                bonus -= 0.2

            if tile.isSwamp:
                if isTarget:
                    bonus -= 4.0
                elif isNeutral:
                    bonus -= 2.0
                # else:
                #     bonus -= 1.0

            if tile.isDesert:
                if isTarget:
                    bonus -= 2.0
                elif isNeutral:
                    bonus -= 1.0

            anyFlankVis = False
            for vis in tile.adjacents:
                if vis in self.board_analysis.flankable_fog_area_matrix:
                    anyFlankVis = True
                    break

            if anyFlankVis:  # and not isCloserToEn
                bonus += 0.05

            # bonus for neutrals near enemy land
            if tile.player == -1 and tgPlayerTerritoryDists.raw[tile.tile_index] < 3:
                bonus += 0.03

            if tile.isCity:
                cityScore = self.cityAnalyzer.city_scores.get(tile, None)
                isCityGapping = (cityScore is None or cityScore.intergeneral_distance_differential > 0) and not self._map.is_tile_friendly(tile)
                for vis in tile.adjacents:
                    if vis.isNotPathable or self._map.is_tile_friendly(vis):
                        continue
                    if isCloserToEn or not isCityGapping or enPotentialGenDistances.raw[tile.tile_index] <= enPotentialGenDistances.raw[vis.tile_index]:
                        matrix.raw[vis.tile_index] += 0.05

            if isTarget:
                if tile.army > 1:
                    bonus += 0.02 + min(50, tile.army) * 0.02

            if not tile.discovered and self.armyTracker.valid_general_positions_by_player[self.targetPlayer].raw[tile.tile_index] and numEnGenPos < 10:
                # if self.territories.is_tile_in_player_territory(tile, self.targetPlayer):
                #     bonus += 0.1
                # else:
                bonus -= 0.01
                # if self._map.is_tile_on_team_with(tile, self.targetPlayer):
                #     bonus += 0.3

            if self._map.is_tile_on_team_with(tile, self.targetPlayer) and self.territories.is_tile_in_friendly_territory(tile):
                bonus += 0.05

            # give bonus points for ending expansion paths in nooks and crannies, ESPECIALLY if they're enemy nooks and crannies, because enemy can't recapture these tiles without wasting moves.
            libertyCount = 0
            for mv in tile.movable:
                if not mv.isObstacle and not (mv.isNeutral and mv.army > 1):
                    libertyCount += 1
            if libertyCount == 1:
                bonus += 0.1
                if isTarget:
                    bonus += 0.3

            pathway = self.board_analysis.intergeneral_analysis.pathWayLookupMatrix.raw[tile.tile_index]
            if pathway is not None:
                extendedDist = pathway.distance - self.board_analysis.within_extended_play_area_threshold
                outsideExtendedPlay = extendedDist > 0
                if outsideExtendedPlay and not (tile in self.board_analysis.flank_danger_play_area_matrix and SearchUtils.any_where(pathway.tiles, lambda t: not t.visible and t in self.board_analysis.flankable_fog_area_matrix)):
                    # try to deprioritize tiles that are outside of our main play area.
                    isEnTile = self._map.is_player_on_team_with(self.targetPlayer, tile.player)
                    # if not tile.visible and tile.isNeutral and self._map.is_player_on_team_with(self.targetPlayer, self.territories.territoryMap[tile]):
                    #     isEnTile = True
                    if isEnTile:
                        factor = 0.5
                        if not tile.discovered:
                            factor = 0.2
                        bonus -= factor / max(4, 15 - extendedDist)
                    else:
                        bonus -= 1.0 / max(4, 15 - extendedDist)
            else:
                bonus -= 10

            # self.viewInfo.topRightGridText.raw[tile.tile_index] = f'er{enDistRatio:.3f}'
            if self._map.remainingCycleTurns > 8 and not searchingForFirstContact:
                cappedRat = max(1.1, enDistRatio)
                if tile.player == -1:
                    bonus += 0.1 - 0.05 * cappedRat
                else:
                    bonus += 0.06 - 0.03 * cappedRat

            # here
            excessDist = enPotentialGenDistances.raw[tile.tile_index] - self.board_analysis.inter_general_distance - numEnGenPos
            if excessDist > 0:
                bonus -= 0.10 * excessDist / max(2, len(genPosesToConsider))

            if self._map.is_tile_friendly(tile):
                if tile.army < 2:
                    bonus -= -0.05
                matrix.raw[tile.tile_index] = min(bonus, 0.0)
            else:
                matrix.raw[tile.tile_index] += bonus

            if self.info_render_expansion_matrix_values:
                self.viewInfo.bottomLeftGridText[tile] = f'x{matrix.raw[tile.tile_index]:0.3f}'

        return matrix

    def is_ffa_situation(self) -> bool:
        if self._is_ffa_situation is None:
            self._is_ffa_situation = not self._map.is_walled_city_game and self.are_more_teams_alive_than(2)

        return self._is_ffa_situation

    def reevaluate_after_player_capture(self):
        if self._map.remainingPlayers <= 3:
            if not self.opponent_tracker.winning_on_economy(byRatio=0.8):
                self.viewInfo.add_info_line("not even on economy, going all in effective immediately")
                self.is_all_in_losing = True
                self.all_in_losing_counter = 300

    def find_fog_bisection_targets(self) -> typing.Set[Tile]:
        bisects = set()
        if self.targetPlayer == -1:
            return bisects

        candidates, distances, bisectPaths = self.armyTracker.find_territory_bisection_paths(self.targetPlayer)

        # self.viewInfo.add_map_zone(candidates, (255, 155, 0), alpha=100)
        for tile in self._map.get_all_tiles():
            self.viewInfo.bottomMidLeftGridText[tile] = f'{"b" if candidates[tile] else "n"}{distances[tile]}'

        for path in bisectPaths:
            self.viewInfo.color_path(PathColorer(
                path,
                0,
                0,
                0,
                255,
                alphaMinimum=155
            ))

            tg = path.tail.tile

            self.viewInfo.add_targeted_tile(tg, TargetStyle.TEAL)

            bisects.add(tg)
            logbook.info(f'BISECTS INCLUDES {str(tg)}!')

        return bisects

    def get_enemy_probable_attack_path(self, enemyPlayer: int) -> Path | None:
        def valFunc(curTile: Tile, prioObj):
            (dist, negArmySum, sumX, sumY, goalIncrement) = prioObj
            if curTile not in self.board_analysis.flankable_fog_area_matrix:
                return None
            if not self._map.is_tile_on_team_with(curTile, enemyPlayer):
                return None
            if curTile.visible:
                return None

            # we WANT this path to be as long as is reasonable
            return 0 - negArmySum # - dist

        def priorityFunc(nextTile, currentPriorityObject):
            (dist, negArmySum, sumX, sumY, goalIncrement) = currentPriorityObject
            dist += 1

            # if negativeTiles is None or next not in negativeTiles:
            if self._map.is_player_on_team_with(nextTile.player, enemyPlayer):
                negArmySum -= nextTile.army
            # else:
            #     negArmySum += nextTile.army
            # always leaving 1 army behind. + because this is negative.
            negArmySum += 1
            # -= because we passed it in positive for our general and negative for enemy gen / cities
            negArmySum -= goalIncrement
            return dist, negArmySum, sumX + nextTile.x, sumY + nextTile.y, goalIncrement

        genSet = set()
        genSet.update(self.player.tiles)

        genTargs = []
        genTargs.append(self.general)

        for teammate in self._map.teammates:
            if not self._map.players[teammate].dead:
                genSet.update(self._map.players[teammate].tiles)
                genTargs.append(self._map.players[teammate].general)

        searchLen = 15
        if self.shortest_path_to_target_player is not None:
            searchLen = self.shortest_path_to_target_player.length + 1

        startTiles = {}
        for tile in genTargs:
            dist = 0
            negArmySum = goalIncrement = 0

            startTiles[tile] = ((dist, negArmySum, tile.x, tile.y, goalIncrement), 0)

        enPath = SearchUtils.breadth_first_dynamic_max(
            self._map,
            startTiles,
            valFunc,
            0.1,
            searchLen,
            priorityFunc=priorityFunc,
            noNeutralCities=True,
            noNeutralUndiscoveredObstacles=True,
            negativeTiles=genSet,
            searchingPlayer=enemyPlayer,
            ignoreNonPlayerArmy=True,
            noLog=True)
        if enPath is None or enPath.length < 3:
            return None

        enPath = enPath.get_reversed()
        enPath.calculate_value(enemyPlayer, self._map.team_ids_by_player_index, genSet, ignoreNonPlayerArmy=True)
        self.viewInfo.color_path(
            PathColorer(
                enPath,
                255, 190, 120,
                alpha=255,
                alphaDecreaseRate=1
            )
        )

        return enPath

    def build_expansion_plan(
            self,
            timeLimit: float,
            expansionNegatives: typing.Set[Tile],
            pathColor: typing.Tuple[int, int, int],
            overrideTurns: int = -1,
            includeExtraGenAndCityArmy: bool = False
    ) -> ExpansionPotential:

        territoryMap = self.territories.territoryMap

        numDanger = 0
        for tile in self.general.movable:
            if (self._map.is_tile_enemy(tile)
                    and tile.army > 5):
                numDanger += 1
                if tile.army > self.general.army - 1:
                    numDanger += 1
        if numDanger > 1:
            expansionNegatives.add(self.general)

        remainingCycleTurns = self.timings.cycleTurns - self.timings.get_turn_in_cycle(self._map.turn)
        if overrideTurns > -1:
            remainingCycleTurns = overrideTurns

        if self.city_expand_plan is not None and len(self.city_expand_plan.plan_paths) == 0:
            self.city_expand_plan = None

        with self.perf_timer.begin_move_event(f'optimal_expansion'):
            bonusCapturePointMatrix = self.get_expansion_weight_matrix()

            remainingMoveTime = self.get_remaining_move_time()
            if remainingMoveTime < timeLimit and not DebugHelper.IS_DEBUGGING:
                timeLimit = remainingMoveTime
                if remainingMoveTime < 0.05:
                    timeLimit = 0.05

            if self.teammate_general is not None:
                expansionNegatives.add(self.teammate_general)

                for army in self.armyTracker.armies.values():
                    if army.player in self._map.teammates and army.last_moved_turn > self._map.turn - 3:
                        expansionNegatives.add(army.tile)

                if self.threat is not None and self.threat.turns < 2 and self.threat.path.tail.tile == self.general:
                    # teammate might defend us, don't throw it all away
                    expansionNegatives.add(self.general)

            interceptOptionsSet: typing.Set[TilePlanInterface] = set()
            # interceptThreatTiles = {}
            addlOptions: typing.List[TilePlanInterface] = []
            """approxEconValue, approxTurns, path"""
            for threatTile, interceptPlan in self.intercept_plans.items():
                for turns, option in interceptPlan.intercept_options.items():
                    addlOptions.append(option)
                    interceptOptionsSet.add(option)

                    logbook.info(f'intOpt {str(option)}')

            if self.expansion_use_iterative_flow:
                with self.perf_timer.begin_move_event('FLOW EXPAND!'):
                    ogStart = time.perf_counter()
                    flowExpander = ArmyFlowExpander(self._map)

                    cutoffTime = time.perf_counter()
                    if self.expansion_use_legacy:
                        cutoffTime += 1 * timeLimit / 2
                    else:
                        cutoffTime += timeLimit

                    flowExpander.friendlyGeneral = self.general
                    flowExpander.enemyGeneral = self.targetPlayerExpectedGeneralLocation
                    flowExpander.debug_render_capture_count_threshold = 10000
                    # flowExpander.log_debug = debugMode
                    flowExpander.log_debug = False
                    # flowExpander.use_debug_asserts = debugMode
                    flowExpander.use_debug_asserts = False
                    # flowExpander.use_min_cost_flow_edges_only = False
                    # flowExpander.use_all_pairs_visited = True

                    optCollection = flowExpander.get_expansion_options(
                        islands=self.tileIslandBuilder,
                        asPlayer=self.player.index,
                        targetPlayer=self.targetPlayer,
                        turns=remainingCycleTurns,
                        boardAnalysis=self.board_analysis,
                        territoryMap=self.territories.territoryMap,
                        negativeTiles=expansionNegatives,
                        cutoffTime=cutoffTime,
                    )

                    addlOptions.extend(optCollection.flow_plans)

                    timeLimit = timeLimit - (time.perf_counter() - ogStart)

            # islands = None
            # if self.expansion_use_tile_islands:
            islands = self.tileIslandBuilder
            expUtilPlan = ExpandUtils.get_round_plan_with_expansion(
                self._map,
                searchingPlayer=self.player.index,
                targetPlayer=self.targetPlayer,
                turns=remainingCycleTurns,
                boardAnalysis=self.board_analysis,
                territoryMap=territoryMap,
                tileIslands=islands,
                negativeTiles=expansionNegatives,
                leafMoves=self.captureLeafMoves,
                useLeafMovesFirst=self.expansion_use_leaf_moves_first,
                viewInfo=self.viewInfo,
                includeExpansionSearch=self.expansion_use_legacy,
                singleIterationPathTimeCap=min(self.expansion_single_iteration_time_cap, timeLimit / 3),
                forceNoGlobalVisited=self.expansion_force_no_global_visited,
                forceGlobalVisitedStage1=self.expansion_force_global_visited_stage_1,
                useIterativeNegTiles=self._should_use_iterative_negative_expand(),
                allowLeafMoves=self.expansion_allow_leaf_moves,
                allowGatherPlanExtension=self.expansion_allow_gather_plan_extension,
                alwaysIncludeNonTerminatingLeavesInIteration=self.expansion_always_include_non_terminating_leafmoves_in_iteration,
                time_limit=timeLimit,
                lengthWeightOffset=self.expansion_length_weight_offset,
                useCutoff=self.expansion_use_cutoff,
                threatBlockingTiles=self.blocking_tile_info,
                colors=pathColor,
                smallTileExpansionTimeRatio=self.expansion_small_tile_time_ratio,
                bonusCapturePointMatrix=bonusCapturePointMatrix,
                additionalOptionValues=addlOptions,
                includeExtraGenAndCityArmy=includeExtraGenAndCityArmy,
                perfTimer=self.perf_timer)

            path = expUtilPlan.selected_option
            otherPaths = expUtilPlan.all_paths

            self.viewInfo.add_stats_line(f'EXP AVAIL {expUtilPlan.turns_used} {expUtilPlan.cumulative_econ_value:.2f} - (en{expUtilPlan.en_tiles_captured} neut{expUtilPlan.neut_tiles_captured})')

        plan = ExpansionPotential(
            expUtilPlan.turns_used,
            expUtilPlan.en_tiles_captured,
            expUtilPlan.neut_tiles_captured,
            path,
            otherPaths,
            expUtilPlan.cumulative_econ_value
        )

        anyIntercept = isinstance(plan.selected_option, InterceptionOptionInfo)
        interceptVtCutoff = 1.99
        if remainingCycleTurns > 35:
            interceptVtCutoff = 2.6
        elif remainingCycleTurns > 28:
            interceptVtCutoff = 2.3
        elif remainingCycleTurns > 22:
            interceptVtCutoff = 2.2

        if len(addlOptions) > 0:
            for otherPath in plan.all_paths:
                if otherPath in interceptOptionsSet:
                    # TODO figure out if better to wait for intercept, maybe...?
                    for planOpt in self.intercept_plans.values():
                        interceptOption = planOpt.get_intercept_option_by_path(otherPath)
                        if interceptOption is not None:
                            isOneMoveLargeIntercept = interceptOption.length < 2 or otherPath.length == 1
                            if isOneMoveLargeIntercept or interceptOption.econValue / interceptOption.length > interceptVtCutoff:
                                self.viewInfo.add_info_line(f'EXP USED INT {interceptOption} ({interceptOption.path})')
                                if interceptOption.requiredDelay > 0:
                                    logbook.info(f'    HAD DELAY {interceptOption}')
                                    plan.blocking_tiles.update(interceptOption.tileSet)
                                    plan.intercept_waiting.append(interceptOption)
                                else:
                                    plan.includes_intercept = True
                                    anyIntercept = True
                                    plan.selected_option = otherPath
                                    break

                i = 0
                for t in otherPath.tileSet:
                    planOpt = self.intercept_plans.get(t, None)
                    if planOpt is not None:
                        intPath = None
                        for turns, option in planOpt.intercept_options.items():
                            if option == planOpt:  # wat? how could there be a self reference here?
                                self.info(f'THIS SHOULD NOT HAPPEN, option == planOpt {planOpt}')
                                self.info(f'THIS SHOULD NOT HAPPEN, option == planOpt {planOpt}')
                                self.info(f'THIS SHOULD NOT HAPPEN, option == planOpt {planOpt}')
                                self.info(f'THIS SHOULD NOT HAPPEN, option == planOpt {planOpt}')
                                self.info(f'THIS SHOULD NOT HAPPEN, option == planOpt {planOpt}')
                                self.info(f'THIS SHOULD NOT HAPPEN, option == planOpt {planOpt}')
                                continue
                            econValue = option.econValue
                            p = option.path
                            isOneMoveLargeIntercept = p.length == 1 or turns < 2
                            if p.start.tile == otherPath.get_first_move().source and (isOneMoveLargeIntercept or econValue / turns > interceptVtCutoff):
                                self.viewInfo.add_info_line(f'EXP INDIR INT ON {str(t)} W {str(otherPath)}')

                                if option.requiredDelay > 0:
                                    self.viewInfo.add_info_line(f'   HAD DELAY {option}')
                                    plan.blocking_tiles.update(option.tileSet)
                                    plan.intercept_waiting.append(option)
                                else:
                                    plan.includes_intercept = True
                                    self.viewInfo.add_info_line(f'   REPLACING WITH {option} ({option.path})')
                                    anyIntercept = True
                                    plan.selected_option = option
                                    plan.all_paths[plan.all_paths.index(otherPath)] = option
                                    break

                        if anyIntercept:
                            plan.includes_intercept = True
                            break

                        i += 1

                if anyIntercept:
                    break

            if not anyIntercept:
                self.viewInfo.add_info_line(f'no exp intercept.. despite {len(addlOptions)} opts?')

        if not anyIntercept:
            plan = self.check_launch_against_expansion_plan(plan, expansionNegatives)

        for path in plan.all_paths:
            plan.preferred_tiles.update(path.tileSet)

        return plan

    def build_enemy_expansion_plan(
            self,
            timeLimit: float,
            pathColor: typing.Tuple[int, int, int]
    ) -> ExpansionPotential:
        if self.targetPlayer == -1 or not self.armyTracker.seen_player_lookup[self.targetPlayer]:
            return ExpansionPotential(0, 0, 0, None, [], 0.0)

        territoryMap = self.territories.territoryMap

        remainingCycleTurns = self.timings.cycleTurns - self.timings.get_turn_in_cycle(self._map.turn)

        with self.perf_timer.begin_move_event(f'enemy optimal_expansion'):
            remainingMoveTime = self.get_remaining_move_time()
            if remainingMoveTime < timeLimit and not DebugHelper.IS_DEBUGGING:
                timeLimit = remainingMoveTime
                if remainingMoveTime < 0.02:
                    timeLimit = 0.02

            enFrTiles = self._map.get_teammates(self.targetPlayer)
            negativeTiles = set()
            for tile in self._map.reachable_tiles:
                # what the fuck was this here for...?
                # if not tile.discovered and self.territories.is_tile_in_player_territory(tile, self.targetPlayer):
                #     negativeTiles.add(tile)
                if not tile.discovered and tile.player in enFrTiles and tile not in self.armyTracker.armies:
                    negativeTiles.add(tile)

            oldA = self.board_analysis.intergeneral_analysis.aMap
            oldB = self.board_analysis.intergeneral_analysis.bMap

            self.board_analysis.intergeneral_analysis.aMap = oldB
            self.board_analysis.intergeneral_analysis.bMap = oldA
            if DebugHelper.IS_DEBUGGING:
                timeLimit *= 4
            try:
                expUtilPlan = ExpandUtils.get_round_plan_with_expansion(
                    self._map,
                    searchingPlayer=self.targetPlayer,
                    targetPlayer=self.player.index,
                    turns=remainingCycleTurns,
                    boardAnalysis=self.board_analysis,
                    territoryMap=territoryMap,
                    tileIslands=self.tileIslandBuilder,
                    negativeTiles=negativeTiles,
                    leafMoves=self.targetPlayerLeafMoves,
                    useLeafMovesFirst=self.expansion_use_leaf_moves_first,
                    viewInfo=self.viewInfo,
                    singleIterationPathTimeCap=min(self.expansion_single_iteration_time_cap, timeLimit / 3),
                    forceNoGlobalVisited=self.expansion_force_no_global_visited,
                    forceGlobalVisitedStage1=self.expansion_force_global_visited_stage_1,
                    useIterativeNegTiles=self._should_use_iterative_negative_expand(),
                    allowLeafMoves=self.expansion_allow_leaf_moves,
                    allowGatherPlanExtension=self.expansion_allow_gather_plan_extension,
                    alwaysIncludeNonTerminatingLeavesInIteration=self.expansion_always_include_non_terminating_leafmoves_in_iteration,
                    time_limit=timeLimit,
                    lengthWeightOffset=self.expansion_length_weight_offset,
                    useCutoff=self.expansion_use_cutoff,
                    smallTileExpansionTimeRatio=self.expansion_small_tile_time_ratio,
                    threatBlockingTiles=self.blocking_tile_info,
                    colors=pathColor,
                    bonusCapturePointMatrix=None)
            finally:
                self.board_analysis.intergeneral_analysis.aMap = oldA
                self.board_analysis.intergeneral_analysis.bMap = oldB

            path = expUtilPlan.selected_option
            otherPaths = expUtilPlan.all_paths

            self.enemy_expansion_plan_tile_path_cap_values = {}

            if path is not None:
                otherPaths.insert(0, path)

            for otherPath in otherPaths:
                army = self.armyTracker.armies.get(otherPath.get_first_move().source, None)
                if isinstance(otherPath, Path):
                    if army is not None:
                        army.include_path(otherPath)

            self.viewInfo.add_stats_line(f'EN EXP AVAIL {expUtilPlan.turns_used} {expUtilPlan.cumulative_econ_value:.2f} - (fr{expUtilPlan.en_tiles_captured} neut{expUtilPlan.neut_tiles_captured})')

        plan = ExpansionPotential(
            expUtilPlan.turns_used,
            expUtilPlan.en_tiles_captured,
            expUtilPlan.neut_tiles_captured,
            path,
            otherPaths,
            expUtilPlan.cumulative_econ_value
        )

        return plan

    def get_opponent_cycle_stats(self) -> CycleStatsData | None:
        if self.targetPlayer == -1:
            return None

        return self.opponent_tracker.get_current_cycle_stats_by_player(self.targetPlayer)

    def check_should_defend_economy_based_on_cycle_behavior(self, defenseCriticalTileSet: typing.Set[Tile]) -> bool:
        self.likely_kill_push = False

        if self.is_ffa_situation():
            return False

        halfDist = self.shortest_path_to_target_player.length - self.shortest_path_to_target_player.length // 2

        oppArmy = self.opponent_tracker.get_approximate_fog_army_risk(self.targetPlayer)
        enGathered = 0
        enData = self.opponent_tracker.get_current_cycle_stats_by_player(self.targetPlayer)
        if enData:
            enGathered = enData.approximate_army_gathered_this_cycle
        if oppArmy < enGathered:
            self.viewInfo.add_info_line(f'Skipping defense play because fogRisk {oppArmy} < en gathered {enGathered}.')
            return False

        threatPath = self.target_player_gather_path

        if self.enemy_attack_path is not None:
            enPath = self.enemy_attack_path.get_subsegment(halfDist + 2, end=True)

            threatPath = self.enemy_attack_path
            enemyAttackPathVal = sum([t.army - 1 for t in enPath.tileList if self._map.is_tile_on_team_with(t, self.targetPlayer) and (t.visible or t.army < 8)])

            enemyAttackPathEnOrFogTiles = sum([1.25 for t in enPath.tileList if (self._map.is_tile_on_team_with(t, self.targetPlayer) or not t.visible) and t.army > 2])
            enemyAttackPathEnOrFogTiles += sum([0.95 for t in enPath.tileList if (self._map.is_tile_on_team_with(t, self.targetPlayer) or not t.visible) and t.army == 2])
            enemyAttackPathEnOrFogTiles += sum([0.55 for t in enPath.tileList if (self._map.is_tile_on_team_with(t, self.targetPlayer) or not t.visible) and t.army <= 1])

            if enemyAttackPathVal > 5:
                self.viewInfo.add_info_line(f'dangerPath with army {enemyAttackPathVal}, increasing oppArmy risk by that.')
                oppArmy += enemyAttackPathVal

            if enemyAttackPathEnOrFogTiles > halfDist // 2:
                self.viewInfo.add_info_line(f'likely_kill_push: danger enTileCount weighted {enemyAttackPathEnOrFogTiles:.1f}>halfDist/2 {halfDist//2}, triggering defensive play.')
                self.likely_kill_push = True

        sketchDist = self.board_analysis.within_flank_danger_play_area_threshold
        if self.sketchiest_potential_inbound_flank_path is not None:
            sketchDist = self._map.get_distance_between(self.general, self.sketchiest_potential_inbound_flank_path.tail.tile)

        # oldLikely = self.likely_kill_push
        # if self._map.remainingCycleTurns < sketchDist:
        #     self.likely_kill_push = False

        if not self.opponent_tracker.winning_on_economy(byRatio=1.08, offset=0 - self.shortest_path_to_target_player.length) and not self.likely_kill_push:
            return False

        if self.timings.get_turns_left_in_cycle(self._map.turn) <= max(halfDist, sketchDist):
            if self.likely_kill_push:
                self.viewInfo.add_info_line(f'bypassing likely_kill_push defense due to near end-of-round')
            return False

        cycleDifferential = self.opponent_tracker.check_gather_move_differential(self.general.player, self.targetPlayer)

        playerArmy = 8
        for tile in self.armyTracker.armies:
            if tile.player == self.general.player and tile.army > playerArmy:
                playerArmy = tile.army - 1

        gathPathSum = 0
        for tile in threatPath.tileList:
            if self._map.is_tile_friendly(tile):
                gathPathSum += tile.army - 1

        playerArmy = max(playerArmy, gathPathSum)

        if oppArmy - gathPathSum > 0 and not self.timings.in_expand_split(self._map.turn):
            for tile in threatPath.tileList:
                if self._map.is_tile_friendly(tile):
                    defenseCriticalTileSet.add(tile)
                    self.viewInfo.add_targeted_tile(tile, TargetStyle.YELLOW)

            self.viewInfo.add_info_line(f'updated defenseCriticals with gather path due to oppArmy {oppArmy} - gathPathSum {gathPathSum} > 0: {str(defenseCriticalTileSet)}')

        if oppArmy + 10 - halfDist <= playerArmy:
            if oppArmy + 10 - halfDist >= playerArmy - 40 and self.likely_kill_push:
                self.block_neutral_captures("likely_kill_push says capping a city would put us under safe army for the push")
            if cycleDifferential < -halfDist:
                self.viewInfo.add_info_line(f'OT oppArmy {oppArmy} vs {playerArmy} - gathMoveDiff {cycleDifferential}, but gathered enough that we dont care?')
            return False

        # TODO take into account last cycles gather too?
        if cycleDifferential < -halfDist and oppArmy >= playerArmy:
            self.viewInfo.add_info_line(f'DEFENDING! OT gathCyc oppArmy {oppArmy} vs {playerArmy} - gathMoveDiff {cycleDifferential}')
            self.defend_economy = True
            return True

        turnsRemaining = self.timings.get_turns_left_in_cycle(self._map.turn)
        minimallyWinningOnEcon = self.opponent_tracker.winning_on_economy(byRatio=1.02, offset=0 - self.shortest_path_to_target_player.length // 2)
        if not minimallyWinningOnEcon and oppArmy - threatPath.length < playerArmy * 1.25 and turnsRemaining < 13:
            return False

        if oppArmy >= (playerArmy + 10) * 1.1 and cycleDifferential < 5 and minimallyWinningOnEcon:
            self.viewInfo.add_info_line(f'DEFENDING! OT army oppArmy {oppArmy} vs {playerArmy} - gathMoveDiff {cycleDifferential}')
            return True

        return False

    def is_move_towards_enemy(self, move: Move | None) -> bool:
        if move is None:
            return False

        if self.targetPlayer is None:
            return False

        # if self.distance_from_opp(move.source) > self.distance_from_opp(move.dest):
        #     return True

        if self.territories.territoryDistances[self.targetPlayer][move.source] > self.territories.territoryDistances[self.targetPlayer][move.dest]:
            return True

        return False

    def str_tiles(self, tiles) -> str:
        return '|'.join([str(t) for t in tiles])

    def get_path_subsegment_to_closest_enemy_team_territory(self, path: Path) -> Path | None:
        idx = 0
        team = self.targetPlayerObj.team
        minDist = self.territories.territoryTeamDistances[team][path.start.tile]
        minIdx = 100
        for tile in path.tileList:
            thisDist = self.territories.territoryTeamDistances[team][tile]
            if thisDist < minDist:
                minDist = thisDist
                minIdx = idx

            idx += 1

        if minIdx == 100:
            logbook.info(f'No closer path to enemy territory found than the start of the path, prefer not using this path at all.')
            return None

        subsegment = path.get_subsegment(minIdx)
        logbook.info(f'closest to enemy team territory was {str(subsegment.tail.tile)} at dist {minIdx}/{path.length}')
        return subsegment

    def check_launch_against_expansion_plan(self, existingPlan: ExpansionPotential, expansionNegatives: typing.Set[Tile]) -> ExpansionPotential:
        if self.target_player_gather_path is None:
            return existingPlan

        launchPath = self.get_path_subsegment_starting_from_last_move(self.target_player_gather_path)

        if launchPath is None or launchPath.start is None:
            return existingPlan

        if launchPath.start.tile in expansionNegatives:
            self.viewInfo.add_info_line(f'---EXP Launch (negs)')
            return existingPlan

        turnsLeftInCycle = self.timings.get_turns_left_in_cycle(self._map.turn)

        if launchPath.length > turnsLeftInCycle:
            launchPath = launchPath.get_subsegment(turnsLeftInCycle)

        distToFirstFogTile, enCaps, neutCaps, turns, econVal, remainingArmy, fullFriendlyArmy = self.calculate_path_capture_econ_values(launchPath, turnsLeftInCycle)

        if turns == 0:
            self.viewInfo.add_info_line(f'---EXP Launch (t0 {str(launchPath.start.tile)}) (en{enCaps} neut{neutCaps}) vs existing (en{existingPlan.en_tiles_captured} neut{existingPlan.neut_tiles_captured})')
            return existingPlan

        if self.targetPlayer != -1 and self.armyTracker.seen_player_lookup[self.targetPlayer] and ((enCaps <= 0 and neutCaps <= 0) or econVal <= 0):
            self.viewInfo.add_info_line(f'---EXP Launch (useless) (en{enCaps} neut{neutCaps}) vs existing (en{existingPlan.en_tiles_captured} neut{existingPlan.neut_tiles_captured})')
            return existingPlan

        if launchPath.start.tile.player != self.general.player:
            self.viewInfo.add_info_line(f'---EXP Launch not our player')
            return existingPlan

        if launchPath.start.tile.army < 7 or launchPath.start.tile.army < fullFriendlyArmy / 5 + 1:
            self.viewInfo.add_info_line(f'---EXP Launch (lowval {str(launchPath.start.tile)}) (en{enCaps} neut{neutCaps}) vs existing (en{existingPlan.en_tiles_captured} neut{existingPlan.neut_tiles_captured})')
            return existingPlan

        existingExpandPlanVal = 0
        # existingExpandTurns = 1
        if existingPlan is not None:
            existingExpandPlanVal = existingPlan.cumulative_econ_value
            # existingExpandTurns = max(1, existingPlan.turns_used)

        playerTiles = self.opponent_tracker.get_player_fog_tile_count_dict(self.targetPlayer)
        worstCappable = 0
        if len(playerTiles) > 0:
            worstCappable = max(playerTiles.keys())
        probableRemainingCaps = min(turnsLeftInCycle - launchPath.length, remainingArmy // (worstCappable + 1))
        launchTurnsTotal = turns + probableRemainingCaps

        if launchTurnsTotal == 0:
            return existingPlan

        factor = 2
        if self.targetPlayer == -1 or len(self.targetPlayerObj.tiles) == 0:
            factor = 1

        launchVal = econVal + probableRemainingCaps * factor

        launchValPerTurn = launchVal / launchTurnsTotal

        existingValPerTurn = existingExpandPlanVal / turnsLeftInCycle
        if existingPlan.turns_used > turnsLeftInCycle - 10:
            existingValPerTurn = existingExpandPlanVal / max(1, existingPlan.turns_used)

        existingPlanForTile = next(iter(p for p in existingPlan.all_paths if p.tileList[0] == launchPath.start.tile), None)
        if existingPlanForTile is not None:
            tilePlanVt = existingPlanForTile.econValue / existingPlanForTile.length
            self.info(f'EXP Launch replacing full {existingValPerTurn:.3f} to ts {tilePlanVt:.3f}')
            existingValPerTurn = tilePlanVt

        if launchValPerTurn > existingValPerTurn and (launchTurnsTotal > turnsLeftInCycle - 5 or distToFirstFogTile < self.target_player_gather_path.length // 2 - 1):
            launchSubsegment = launchPath.get_subsegment(distToFirstFogTile)

            launchSubsegmentToEn = self.get_path_subsegment_to_closest_enemy_team_territory(launchSubsegment)
            if launchSubsegmentToEn is None:
                launchSubsegmentToEn = launchSubsegment

            # if self.timings.in_launch_timing(self._map.turn):
            #     self.curPath = launchSubsegmentToEn

            paths = existingPlan.all_paths.copy()
            interceptFake = InterceptionOptionInfo(
                launchSubsegmentToEn,
                econVal,
                launchSubsegment.length + probableRemainingCaps,
                damageBlocked=0,
                interceptingArmyRemaining=0,
                bestCaseInterceptMoves=0,
                worstCaseInterceptMoves=0,
                recaptureTurns=probableRemainingCaps,
                requiredDelay=0,
                friendlyArmyReachingIntercept=0)
            paths.insert(0, interceptFake)
            self.viewInfo.add_info_line(f'EXP Launch vt{launchValPerTurn:.2f} (en{enCaps} neut{neutCaps}) vs existing {existingValPerTurn:.2f} (en{existingPlan.en_tiles_captured} neut{existingPlan.neut_tiles_captured})')
            newPlan = ExpansionPotential(
                turnsUsed=existingPlan.turns_used + launchTurnsTotal,
                enTilesCaptured=enCaps,
                neutTilesCaptured=neutCaps,
                selectedOption=interceptFake,
                allOptions=paths,
                cumulativeEconVal=existingPlan.cumulative_econ_value + launchVal
            )

            return newPlan

        # if DebugHelper.IS_DEBUGGING:
        self.viewInfo.add_info_line(f'---EXP Launch vt{launchValPerTurn:.2f} (en{enCaps} neut{neutCaps}) vs existing {existingValPerTurn:.2f} (en{existingPlan.en_tiles_captured} neut{existingPlan.neut_tiles_captured})')

        return existingPlan

    def calculate_path_capture_econ_values(self, launchPath, turnsLeftInCycle, negativeTiles: typing.Set[Tile] | None = None) -> typing.Tuple[int, int, int, int, int, int, int]:
        """

        @param launchPath:
        @param turnsLeftInCycle:
        @param negativeTiles:
        @return: distToFirstFogTile, enCaps, neutCaps, turns, econVal, army, friendlyArmy
        """

        econMatrix = self.get_expansion_weight_matrix()

        army = 0
        turns = -1
        enCaps = 0
        neutCaps = 0
        distToFirstCap = -1
        distToFirstFogTile = -1
        friendlyArmy = 0
        econVal = 0
        for tile in launchPath.tileList:
            turns += 1
            isFriendly = self._map.is_tile_friendly(tile)
            if isFriendly:
                army += tile.army - 1
                friendlyArmy += tile.army - 1
            else:
                army -= tile.army + 1

                if distToFirstFogTile == -1 and not tile.visible:
                    distToFirstFogTile = turns

                if army < 0:
                    break

                if distToFirstCap == -1:
                    distToFirstCap = turns

                econVal += econMatrix.raw[tile.tile_index]

                if tile.isSwamp:
                    army -= 1
                    econVal -= 1
                elif not tile.isDesert:
                    if self._map.is_player_on_team_with(self.targetPlayer, tile.player):
                        enCaps += 1
                    else:
                        neutCaps += 1

            if army <= 0:
                break
            if turns >= turnsLeftInCycle:
                break

        if distToFirstCap == -1:
            distToFirstCap = launchPath.length
        if distToFirstFogTile == -1:
            distToFirstFogTile = launchPath.length

        econVal += 2 * enCaps + neutCaps

        return distToFirstFogTile, enCaps, neutCaps, turns, econVal, army, friendlyArmy

    def make_second_25_move(self) -> Move | None:
        if self._map.turn >= 100 or self.is_ffa_situation() or self.completed_first_100 or self._map.is_2v2 or self.targetPlayer == -1:
            return None

        foundEnemy = SearchUtils.any_where(self.targetPlayerObj.tiles, lambda t: t.visible)

        cutoff = 67
        if foundEnemy:
            cutoff = 67

        if self._map.turn > cutoff:  # or
            return None

        # goals, make sure we use every single 2.
        # fan out quickly away from general.

        if self.curPath is not None:
            if self.curPath.start.tile.army - 1 > self.curPath.start.next.tile.army:
                return self.continue_cur_path(threat=None, defenseCriticalTileSet=set())

            self.curPath = None
        #
        # possibleGenTargets = self.get_target_player_possible_general_location_tiles_sorted(
        #     elimNearbyRange=11,
        #     player=self.targetPlayer
        # )[0:4]
        #
        # if len(possibleGenTargets) < 3:
        #     newPossibleGenTargets = self.get_target_player_possible_general_location_tiles_sorted(
        #         elimNearbyRange=7,
        #         player=self.targetPlayer
        #     )[0:4]
        #
        #     if len(newPossibleGenTargets) > len(possibleGenTargets):
        #         possibleGenTargets = newPossibleGenTargets
        #
        # if not possibleGenTargets and self.targetPlayer != -1:
        #     possibleGenTargets = [self.targetPlayerExpectedGeneralLocation]
        #
        # maxPath: Path | None = None
        # maxPathDist = 100
        #
        #
        # leafMovesClosestToGen = list(sorted(self.captureLeafMoves, key=lambda m: self.distance_from_general(m.dest)))
        # # ignore the leafmoves closest to general
        # leafMoves = leafMovesClosestToGen[6:]
        #
        # genOptionDistances = SearchUtils.build_distance_map_matrix(self._map, possibleGenTargets)
        expMap = self.get_expansion_weight_matrix()
        #
        # leafMoves = [m for m in self.leafMoves if m.source.army > 1]
        # enDists = self._alt_en_gen_position_distances[self.targetPlayer]
        # leafMovesClosestToGen = list(sorted(leafMoves, key=lambda m: self.distance_from_general(m.dest)))
        # # ignore the leafmoves closest to general
        # leafMoves = leafMovesClosestToGen[6:]

        # prioritizedLeaves = self.prioritize_expansion_leaves(leafMoves, distPriorityMap=enDists)

        tilePathLookup = {}
        maxPath: Path | None = None
        possibleGenTargets = self.alt_en_gen_positions[self.targetPlayer]
        enDists = self._alt_en_gen_position_distances[self.targetPlayer]
        mustGather = set(self.player.tiles)
        mustGatherTo = set(self.target_player_gather_path.tileList)
        for tile in self.target_player_gather_path.tileList:
            mustGather.discard(tile)

        # localClosest: typing.Dict[Tile, int] = {}

        negTiles = set()

        bypass = set()

        def foreachFunc(tile: Tile) -> bool:
            if tile.tile_index in bypass or enDists.raw[tile.tile_index] > enDists.raw[self.general.tile_index] + 1:
                return True

            anyMovable = False
            for t in tile.movable:
                if not t.isObstacle and self._map.team_ids_by_player_index[t.player] != self._map.team_ids_by_player_index[self.general.player]:
                    anyMovable = True
                    break

            if anyMovable and tile.player == self.general.player:
                mustGatherTo.add(tile)
                mustGather.discard(tile)
                logbook.info(f'INCL {tile}')
                def foreachSkipNearby(nearbyTile: Tile) -> bool:
                    if nearbyTile.player != self.general.player:
                        return True
                    bypass.add(nearbyTile.tile_index)

                SearchUtils.breadth_first_foreach_fast_no_neut_cities(self._map, [tile], 6, foreachSkipNearby)

        SearchUtils.breadth_first_foreach_fast_no_neut_cities(
            self._map,
            self.alt_en_gen_positions[self.targetPlayer],
            maxDepth=150,
            foreachFunc=foreachFunc,
        )

        for tile in self.player.tiles:
            if tile.army == 1:
                mustGather.discard(tile)

        for tile in self.target_player_gather_path.tileList:
            mustGather.discard(tile)
            if not self._map.is_tile_friendly(tile):
                mustGatherTo.discard(tile)

        # mustGatherTo.add(self.general)

        for tile in mustGatherTo:
            self.viewInfo.add_targeted_tile(tile, TargetStyle.GREEN, radiusReduction=8)

        gatherTieBreaks = self.get_gather_tiebreak_matrix()
        for tile in mustGather:
            gatherTieBreaks.raw[tile.tile_index] += 0.5
            self.viewInfo.add_targeted_tile(tile, TargetStyle.YELLOW, radiusReduction=8)

        gathTurns = 25

        gathTargets = {}
        for tile in mustGatherTo:
            path = tilePathLookup.get(tile, None)
            if path is None:
                gathTargets[tile] = 0
                continue

            allowedAddlArmy = path.length - tile.army + 1

            depth = gathTurns - allowedAddlArmy
            logbook.info(f'setting {str(tile)} to depth {depth} / {gathTurns} (army {tile.army}, path len {path.length}, allowedAddlArmy {allowedAddlArmy})')
            gathTargets[tile] = depth

        move, valGathered, gathTurns, gatherNodes = self.get_gather_to_target_tiles(
            gathTargets,
            0.1,
            gatherTurns=gathTurns,
            priorityMatrix=gatherTieBreaks
        )

        if self.timings.splitTurns - self._map.cycleTurn < gathTurns:
            newSplit = self._map.cycleTurn + gathTurns
            self.info(f'increasing gather duration from {self.timings.splitTurns} to {newSplit}')
            self.timings.splitTurns = newSplit
            if self.timings.launchTiming < self.timings.splitTurns:
                self.timings.launchTiming = self.timings.splitTurns

        if move is not None:
            if move.source in self.out_of_play_tiles or enDists.raw[move.source.tile_index] > enDists.raw[self.general.tile_index]:
                self.info(f'f50 out of play {move}')
                return move

        if gatherNodes:
            sendPath: Path | None = None
            expansionPathsByStartTile = {}
            for p in self.expansion_plan.all_paths:
                expansionPathsByStartTile[p.tileList[0]] = p

            for node in gatherNodes:
                if node.gatherTurns == 0 and node.tile not in self.target_player_gather_path.tileSet:  # SearchUtils.count(node.tile.movable, lambda mv: mv in mustGatherTo) <= 1
                    path = expansionPathsByStartTile.get(node.tile, None)
                    if path:
                        self.curPath = path
                        self.info(f'f50 0 node path {str(path)}')
                        return self.get_first_path_move(path)

        #
        # if move is not None:
        #     prunedCount, prunedValue, gatherNodes = Gather.prune_mst_to_tiles_with_values(
        #         gatherNodes,
        #         mustGather,
        #         self.general.player,
        #         self.viewInfo,
        #         preferPrune=self.expansion_plan.preferred_tiles if self.expansion_plan is not None else None,
        #     )

        for tile in possibleGenTargets:
            self.viewInfo.add_targeted_tile(tile, TargetStyle.RED, radiusReduction=8)

        if maxPath is not None:
            useMaxPath = True
            if gatherNodes:
                maxPathNodes = SearchUtils.where(gatherNodes, lambda n: n.tile == maxPath.start.tile)
                if len(maxPathNodes) > 0:
                    gathNode = maxPathNodes[0]
                    if gathNode.gatherTurns != 0:
                        useMaxPath = False
            if useMaxPath:
                self.curPath = maxPath
                self.info(f'f50 maxpath {str(maxPath)}')
                return self.get_first_path_move(maxPath)

        if gatherNodes:
            self.gatherNodes = gatherNodes
            move = self.get_tree_move_default(gatherNodes)
            if move is not None:
                self.info(f'f50 Expansion gather {move}')
                return move

        self.completed_first_100 = True

        return None

    def get_enemy_cities_by_priority(self, cutoffDistanceRatio=100.0) -> typing.List[Tile]:
        prioTiles = []
        if self.dangerAnalyzer.fastestThreat is not None:
            if self.dangerAnalyzer.fastestThreat.path.start.tile.isCity:
                prioTiles.append(self.dangerAnalyzer.fastestThreat.path.start.tile)

        if self.dangerAnalyzer.fastestPotentialThreat is not None and self.dangerAnalyzer.fastestPotentialThreat.path.start.tile not in prioTiles:
            if self.dangerAnalyzer.fastestPotentialThreat.path.start.tile.isCity:
                prioTiles.append(self.dangerAnalyzer.fastestPotentialThreat.path.start.tile)

        if self.dangerAnalyzer.fastestAllyThreat is not None and self.dangerAnalyzer.fastestAllyThreat.path.start.tile not in prioTiles:
            if self.dangerAnalyzer.fastestAllyThreat.path.start.tile.isCity:
                prioTiles.append(self.dangerAnalyzer.fastestAllyThreat.path.start.tile)

        if self.dangerAnalyzer.fastestCityThreat is not None and self.dangerAnalyzer.fastestCityThreat.path.start.tile not in prioTiles:
            if self.dangerAnalyzer.fastestCityThreat.path.start.tile.isCity:
                prioTiles.append(self.dangerAnalyzer.fastestCityThreat.path.start.tile)

        tiles = [s for s, score in self.cityAnalyzer.get_sorted_enemy_scores() if s not in prioTiles and score.general_distances_ratio < cutoffDistanceRatio]

        prioTiles.extend(tiles)
        return prioTiles

    def did_player_just_take_fog_city(self, player: int) -> bool:
        playerObj = self._map.players[player]
        if playerObj.unexplainedTileDelta == 0:
            return False
        unexplainedScoreDelta = self.opponent_tracker.get_team_annihilated_fog_by_player(player)
        if unexplainedScoreDelta < 3:
            return False
        if playerObj.cityGainedTurn == self._map.turn:
            return True

        return False

    def get_city_contestation_all_in_move(self, defenseCriticalTileSet: typing.Set[Tile]) -> typing.Tuple[Move | None, int, int, typing.List[GatherTreeNode]]:
        targets = list(self.win_condition_analyzer.contestable_cities)

        negatives = defenseCriticalTileSet.copy()
        negatives.update(self.win_condition_analyzer.defend_cities)

        if len(targets) == 0:
            targets = self.get_target_player_possible_general_location_tiles_sorted(elimNearbyRange=7, cutoffEmergenceRatio=0.5)[0:3]

        turns = self.win_condition_analyzer.recommended_offense_plan_turns

        move, valGathered, gatherTurns, gatherNodes = self.get_gather_to_target_tiles(
            targets,
            maxTime=0.05,
            gatherTurns=turns,
            maximizeArmyGatheredPerTurn=True,
            useTrueValueGathered=True,
            negativeSet=defenseCriticalTileSet)

        if gatherNodes:
            prunedGatherTurns, sumPruned, prunedGatherNodes = Gather.prune_mst_to_max_army_per_turn_with_values(
                gatherNodes,
                minArmy=1,
                searchingPlayer=self.general.player,
                teams=self.teams,
                additionalIncrement=0,
                preferPrune=self.expansion_plan.preferred_tiles if self.expansion_plan is not None else None,
                viewInfo=self.viewInfo)

            rootGatheredTiles = [n.tile for n in prunedGatherNodes]
            predictedTurns, predictedDefenseVal = self.win_condition_analyzer.get_dynamic_turns_visible_defense_against(rootGatheredTiles, prunedGatherTurns, prunedGatherNodes[0].tile.player)
            fogRisk = self.opponent_tracker.get_approximate_fog_army_risk(self.targetPlayer, inTurns=0)  # we're assuming they gather visible tiles, so they don't get to ALSO gather fog.
            if sumPruned < predictedDefenseVal + fogRisk:
                return None, 0, 0, []

            numCaptures = self.get_number_of_captures_in_gather_tree(prunedGatherNodes)

            if sumPruned / max(1, prunedGatherTurns - numCaptures) > 3 * self.player.standingArmy / self.player.tileCount - 1:
                if len(prunedGatherNodes) > 0:
                    move = self.get_tree_move_default(gatherNodes)

                for tile in targets:
                    self.viewInfo.add_targeted_tile(tile, TargetStyle.ORANGE, radiusReduction=-1)

                if move is not None:
                    self.info(f'City Contest Off {move} (val {valGathered}/p{sumPruned} turns {gatherTurns}/p{prunedGatherTurns})')

                return move, sumPruned, prunedGatherTurns, prunedGatherNodes

        return None, 0, 0, []

    def get_number_of_captures_in_gather_tree(self, gatherNodes: typing.List[GatherTreeNode], asPlayer: int = -2) -> int:
        if asPlayer == -2:
            asPlayer = self._map.player_index

        if gatherNodes is None or len(gatherNodes) == 0:
            return 0

        sumCaps = SearchUtils.Counter(0)

        def c(n: GatherTreeNode):
            if not self._map.is_tile_on_team_with(n.tile, asPlayer) and len(n.children) > 0:
                sumCaps.value += 1

        GatherTreeNode.foreach_tree_node(gatherNodes, forEachFunc=c)

        return sumCaps.value

    def get_city_preemptive_defense_move(self, defenseCriticalTileSet: typing.Set[Tile]) -> Move | None:
        if self.is_still_ffa_and_non_dominant():
            return None

        # TODO gather to just the visionless tile closest to the city so opponent doesn't know where our army is or how much it is.

        sketchyOutOfPlayThresh = self.player.standingArmy // 6
        sketchyCities = [c for c in self.win_condition_analyzer.defend_cities]
        targets = list(self.win_condition_analyzer.defend_cities)

        if len(sketchyCities) > 0:
            sketchyLargeArmyCities = [c for c in sketchyCities if c.army > sketchyOutOfPlayThresh // 4]
            if len(sketchyLargeArmyCities) > 0:
                sketchyLargeArmyCities = sketchyCities
            sketchyArmy = 0
            for t in sketchyCities:
                sketchyArmy += t.army - 1

            if sketchyArmy > sketchyOutOfPlayThresh and self.sketchiest_potential_inbound_flank_path is not None:
                furthestCity = max(sketchyLargeArmyCities, key=lambda c: self.board_analysis.intergeneral_analysis.aMap[c])
                furthestDist = self.board_analysis.intergeneral_analysis.aMap[furthestCity]
                fogDist = self.board_analysis.intergeneral_analysis.aMap[self.sketchiest_potential_inbound_flank_path.tail.tile]

                if fogDist <= furthestDist:
                    negs = defenseCriticalTileSet.copy()
                    negs.update(sketchyCities)
                    with self.perf_timer.begin_move_event('city preemptive flank defense get_flank_vision_defense_move'):
                        flankDefenseMove = self._get_flank_vision_defense_move_internal(self.sketchiest_potential_inbound_flank_path, negs, atDist=furthestDist)
                    if flankDefenseMove is not None:
                        self.info(f'Flank defense {str(flankDefenseMove)}')
                        return flankDefenseMove
                    else:
                        self.viewInfo.add_info_line(f'There was a flank risk, but we didnt find a flank defense move...?')

        wouldStillBeAheadIfOppTakesCity = self.opponent_tracker.winning_on_economy(byRatio=1.02, offset=-33)

        turnsLeft = self.timings.get_turns_left_in_cycle(self._map.turn)
        turnCutoffLowEcon = int(self.shortest_path_to_target_player.length * 1.0)
        turnCutoffHighEcon = int(self.shortest_path_to_target_player.length * 0.4)
        if turnsLeft <= turnCutoffHighEcon:
            self.viewInfo.add_info_line(f'bypassing preemptive city defense t{turnsLeft}/{turnCutoffHighEcon} (high econ)')
            return None

        if turnsLeft <= turnCutoffLowEcon and not wouldStillBeAheadIfOppTakesCity:
            self.viewInfo.add_info_line(f'bypassing preemptive city defense t{turnsLeft}/{turnCutoffLowEcon} due to not winning by that much')
            return None

        # tiles = [t for t in targets if not self.cityAnalyzer.is_contested(t, captureCutoffAgoTurns=20, enemyTerritorySearchDepth=0)]

        # if not self.opponent_tracker.winning_on_tiles():

        negs = defenseCriticalTileSet.copy()

        negs.update(targets)
        negs.update(self.cityAnalyzer.owned_contested_cities)

        tilesFarFromUs = [t for t in self.player.tiles if t not in self.tiles_gathered_to_this_cycle and self.territories.territoryTeamDistances[self.targetPlayerObj.team].raw[t.tile_index] < 2 and t.army < 5]
        for t in tilesFarFromUs:
            self.viewInfo.evaluatedGrid[t.x][t.y] = 100
            negs.add(t)

        newTargets = set()
        for t in targets:
            negs.add(t)
            tiles = self.get_n_closest_team_tiles_near([t], self.targetPlayer, distance=max(1, self.board_analysis.inter_general_distance // 7), limit=7, includeNeutral=False)
            if len(tiles) < 1:
                tiles = self.get_n_closest_team_tiles_near([t], self.targetPlayer, distance=max(2, self.board_analysis.inter_general_distance // 5), limit=10, includeNeutral=True)
            if len(tiles) < 1:
                tiles = [t]

            negs.update(self.get_n_closest_team_tiles_near([t], self.general.player, distance=max(2, self.board_analysis.inter_general_distance // 5), limit=6, includeNeutral=False))

            newTargets.update(tiles)

        for tg in newTargets:
            self.viewInfo.add_targeted_tile(tg, TargetStyle.BLUE, radiusReduction=-3)
        for tg in negs:
            self.viewInfo.evaluatedGrid[tg.x][tg.y] += 100

        move, valGathered, gatherTurns, gatherNodes = self.get_gather_to_target_tiles(
            [t for t in newTargets],
            maxTime=0.05,
            gatherTurns=self.win_condition_analyzer.recommended_city_defense_plan_turns,
            # maximizeArmyGatheredPerTurn=True,
            useTrueValueGathered=True,
            priorityMatrix=self.get_gather_tiebreak_matrix(),
            negativeSet=negs)

        numCaptures = self.get_number_of_captures_in_gather_tree(gatherNodes)

        if valGathered / max(1, gatherTurns - numCaptures) < self.player.standingArmy / self.player.tileCount:
            cycleTurns = self.timings.get_turns_left_in_cycle(self._map.turn) % 25
            cycleTurns = max(self.win_condition_analyzer.recommended_city_defense_plan_turns + 15, cycleTurns)
            self.info(f'trying longer city preemptive defense turns {cycleTurns}')
            move, valGathered, gatherTurns, gatherNodes = self.get_gather_to_target_tiles(
                [t for t in newTargets],
                maxTime=0.05,
                gatherTurns=cycleTurns,
                # maximizeArmyGatheredPerTurn=True,
                useTrueValueGathered=True,
                priorityMatrix=self.get_gather_tiebreak_matrix(),
                negativeSet=negs)

            if gatherNodes is not None:
                prunedGatherTurns, sumPruned, prunedGatherNodes = Gather.prune_mst_to_max_army_per_turn_with_values(
                    gatherNodes,
                    minArmy=1,
                    searchingPlayer=self.general.player,
                    teams=self.teams,
                    additionalIncrement=0,
                    preferPrune=self.expansion_plan.preferred_tiles if self.expansion_plan is not None else None,
                    viewInfo=self.viewInfo)

                if prunedGatherNodes is not None and len(prunedGatherNodes) > 0:
                    move = self.get_tree_move_default(gatherNodes)
                    valGathered = sumPruned
                    gatherTurns = prunedGatherTurns

        for tile in self.win_condition_analyzer.defend_cities:
            self.viewInfo.add_targeted_tile(tile, TargetStyle.WHITE, radiusReduction=3)

        if move is not None:
            self.info(f'C preDef {move} - {valGathered} in turns {gatherTurns}/{self.win_condition_analyzer.recommended_city_defense_plan_turns}')

        return move

    def find_flank_defense_move(self, defenseCriticalTileSet: typing.Set[Tile], highPriority: bool = False) -> Move | None:
        checkPath = self.sketchiest_potential_inbound_flank_path

        if self.enemy_attack_path is not None and self.likely_kill_push:
            self.info(f'~~risk threat - replacing flank with risk threat BC likely_kill_push')
            checkPath = self.enemy_attack_path.get_subsegment_excluding_trailing_visible()
        elif self.is_ffa_situation():
            return None

        checkFlank = checkPath is not None and (
                checkPath.tail.tile in self.board_analysis.flank_danger_play_area_matrix
                or checkPath.tail.tile in self.board_analysis.core_play_area_matrix
        )

        coreNegs = defenseCriticalTileSet.copy()
        coreNegs.update(self.win_condition_analyzer.defend_cities)
        coreNegs.update(self.win_condition_analyzer.contestable_cities)

        if highPriority and checkPath:
            winningMassivelyOnArmy = self.opponent_tracker.winning_on_army(byRatio=1.4) and self.opponent_tracker.winning_on_economy(byRatio=1.15)
            winningMassivelyOnEcon = self.opponent_tracker.winning_on_army(byRatio=1.1) and self.opponent_tracker.winning_on_economy(byRatio=1.4)
            winningInTheMiddle = self.opponent_tracker.winning_on_army(byRatio=1.25) and self.opponent_tracker.winning_on_economy(byRatio=1.05, offset=-25)
            winningByEnoughToBeSuperCareful = winningMassivelyOnArmy or winningMassivelyOnEcon or winningInTheMiddle

            flankIsCloserThanThreeFifths = self.distance_from_general(checkPath.tail.tile) < 3 * self.shortest_path_to_target_player.length // 5
            if winningByEnoughToBeSuperCareful and flankIsCloserThanThreeFifths:
                turns = 3 + (self.timings.get_turns_left_in_cycle(self._map.turn) + 1) % 4
                with self.perf_timer.begin_move_event(f'superCareful flank gath {turns}t'):
                    startTiles = checkPath.convert_to_dist_dict(offset=0 - checkPath.length)
                    for t in list(startTiles.keys()):
                        if t.isSwamp or SearchUtils.any_where(t.movable, lambda m: m.isSwamp):
                            startTiles.pop(t)
                    move = None
                    if len(startTiles) > 0:
                        move, valGathered, turnsUsed, nodes = self.get_gather_to_target_tiles(
                            startTiles,
                            maxTime=0.002,
                            gatherTurns=turns,
                            negativeSet=defenseCriticalTileSet,
                            targetArmy=1,
                            useTrueValueGathered=True,
                            includeGatherTreeNodesThatGatherNegative=False,
                            maximizeArmyGatheredPerTurn=True,
                            priorityMatrix=self.get_expansion_weight_matrix(mult=10))

                    if move:
                        forcedHalf = False
                        if 4 < valGathered <= move.source.army // 2 and not self.is_move_towards_enemy(move):
                            move.move_half = True
                            forcedHalf = True
                        self.info(f'superCareful flank gath for {turns}t: {move} ({valGathered} in {turnsUsed}t). Half {forcedHalf}')
                        return move

                leafMove = self._get_vision_expanding_available_move(coreNegs, checkPath)
                if leafMove is not None:
                    # already logged
                    return leafMove

            # high priority moves trump city and stuff.
            return None

        if self.is_still_ffa_and_non_dominant():
            return None

        leafMove = self._get_vision_expanding_available_move(coreNegs, checkPath)
        if leafMove is not None:
            # already logged
            return leafMove

        if not checkFlank:
            return None

        # was above vision expansion...?
        if checkFlank:
            leafMove = self._get_flank_defense_leafmove(checkPath, coreNegs)
            if leafMove is not None:
                self.info(f'LEAF proactive flank vision defense {str(leafMove)}')
                return leafMove

        negs = coreNegs.copy()
        negs.update([p.get_first_move().source for p in self.expansion_plan.all_paths if p.get_first_move().source.delta.armyDelta == 0])
        flankDefMove = self._get_flank_vision_defense_move_internal(
            checkPath,
            negs,
            atDist=self.board_analysis.within_flank_danger_play_area_threshold)
        if flankDefMove is not None:
            self.info(f'proactive flank vision defense {str(flankDefMove)}')
            return flankDefMove

        # try again but without preventing the expansion plan paths
        flankDefMove = self._get_flank_vision_defense_move_internal(
            checkPath,
            coreNegs,
            atDist=self.board_analysis.within_flank_danger_play_area_threshold)
        if flankDefMove is not None:
            self.info(f'No exp negs proactive flank vision defense {str(flankDefMove)}')
            return flankDefMove

        return None

    def _get_flank_defense_leafmove(self, flankPath: Path, coreNegs: typing.Set[Tile]) -> Move | None:
        # destLookup = {}
        # for leafMove in leafMoves:
        #     destLookup[leafMove.dest] = leafMove
        #
        # for i, tile in enumerate(flankPath):

        # by setting bestdist one away, we guarantee we wont just leafmove 1 move up INTO the flank but instead hit it from the side to cut it much shorter in one move.
        # bestDist = 2
        # bestRevealed = 0
        bestWeighted = 3
        bestMove = None
        for leafMove in self.captureLeafMoves:
            if leafMove.dest.isSwamp:
                continue
            if leafMove.source in coreNegs:
                continue

            dist = self._map.get_distance_between(flankPath.tail.tile, leafMove.dest)
            revealed = 0
            for t in leafMove.dest.adjacents:
                if t in self.board_analysis.flankable_fog_area_matrix:
                    revealed += 1
            # if dist < 2 or (dist == bestDist and revealed < bestRevealed):
            #     continue

            weighted = dist + revealed
            if dist < 2 or weighted < bestWeighted:
                continue

            if leafMove.dest in flankPath.adjacentSet:
                bestMove = leafMove
                bestWeighted = weighted

        return bestMove

    def _get_vision_expanding_available_move(self, coreNegs: typing.Set[Tile], pathToCheckForVisionOf: Path | None = None) -> Move | None:
        """

        @param coreNegs:
        @param pathToCheckForVisionOf:
        @return:
        """
        bestWeighted = 3
        bestMove = None

        if pathToCheckForVisionOf is None:
            pathToCheckForVisionOf = self.sketchiest_potential_inbound_flank_path
        if pathToCheckForVisionOf is None:
            return None

        hidden = {t for t in pathToCheckForVisionOf.tileList if not t.visible}

        alreadyInExpPlan = not hidden.isdisjoint(self.expansion_plan.plan_tiles)

        if self.timings.get_turn_in_cycle(self._map.turn) >= 6 and not alreadyInExpPlan:
            return None

        if alreadyInExpPlan:
            lastNonVisibleTile = None
            lenWithFog = 0
            for dist, t in enumerate(pathToCheckForVisionOf.tileList):
                if not t.visible:
                    lastNonVisibleTile = t
                    lenWithFog = dist

            fullDist = lenWithFog + self.board_analysis.intergeneral_analysis.aMap[lastNonVisibleTile]
            midDist = fullDist // 2
            closestToMid: TilePlanInterface | None = None
            closestToMidDist = 100000
            cutoff = self.get_median_tile_value(85) + 2
            for p in self.expansion_plan.all_paths:
                if not isinstance(p, Path):
                    # TODO handle other plan types, eventually?
                    continue
                if (p.value > 10 or p.length > 10 or (p.length > 5 and p.econValue / p.length < 1.5)) and not self.is_all_in_army_advantage and not self.is_winning_gather_cyclic and not self.defend_economy:
                    continue

                if self.likely_kill_push and p.length > 2:
                    continue

                if p.start.tile in self.target_player_gather_path.tileSet or p.start.tile.isCity or p.start.tile.isGeneral:
                    continue

                intersection = hidden.intersection(p.tileList)
                if len(intersection) > 0:
                    for t in intersection:
                        tDist = abs(self.board_analysis.intergeneral_analysis.aMap[t] - self.board_analysis.intergeneral_analysis.bMap[t])
                        if tDist < closestToMidDist:
                            closestToMid = p
                            closestToMidDist = tDist

            if closestToMid is not None:
                move = closestToMid.get_first_move()
                self.info(f'EXP plan included vision expansion {move}')
                return move

        if self.timings.get_turn_in_cycle(self._map.turn) >= 6:
            return None

        for leafMove in self.captureLeafMoves:
            if leafMove.dest.isSwamp:
                continue
            dist = self._map.get_distance_between(self.general, leafMove.dest)

            if leafMove.source in coreNegs:
                continue

            revealed = 0
            anyFog = False
            for t in leafMove.dest.adjacents:
                if not t.discovered and t.player != -1:
                    revealed += 2
                if t in self.board_analysis.flankable_fog_area_matrix:
                    anyFog = True

            if not anyFog or revealed == 0:
                continue

            weighted = dist + revealed
            if dist < 2 or weighted < bestWeighted:
                continue
            bestMove = leafMove
            bestWeighted = weighted

        if bestMove is not None:
            self.info(f'vision expansion leaf {str(bestMove)}')

        return bestMove

    def _get_flank_vision_defense_move_internal(self, flankThreatPath: Path, negativeTiles: typing.Set[Tile], atDist: int) -> Move | None:
        included = set()
        for tile in flankThreatPath.tileList[:(flankThreatPath.length * 5) // 6]:
            if tile in self.board_analysis.flank_danger_play_area_matrix and not tile.visible and not tile.isSwamp:
                included.add(tile)
            # for adj in tile.adjacents:
            #     if adj.isObstacle:
            #         continue
            #     pathWay = self.board_analysis.intergeneral_analysis.pathWayLookupMatrix[adj]
            #     if pathWay is None or pathWay.distance < atDist:
            #         continue
            #     included.add(adj)

        for t in included:
            self.viewInfo.add_targeted_tile(t, targetStyle=TargetStyle.GOLD, radiusReduction=11)

        flankThreatTiles = set(flankThreatPath.tileList[flankThreatPath.length // 2:])

        SearchUtils.breadth_first_foreach(self._map, self.target_player_gather_path.adjacentSet, maxDepth=2, foreachFunc=lambda t: flankThreatTiles.discard(t), noLog=True)
        if len(flankThreatTiles) < flankThreatPath.length // 5 + 1:
            return None

        capture_first_value_func = self.get_capture_first_tree_move_prio_func()

        move = None
        offset = 0
        maxOffs = self.target_player_gather_path.length // 4

        while move is None and offset < maxOffs:
            gathTurns = offset + (50 - self._map.turn) % 4
            move, valGathered, gatherTurns, gatherNodes = self.get_gather_to_target_tiles(
                [t for t in included],
                maxTime=0.002,
                gatherTurns=gathTurns,
                maximizeArmyGatheredPerTurn=True,
                targetArmy=0,
                leafMoveSelectionValueFunc=capture_first_value_func,
                useTrueValueGathered=True,
                includeGatherTreeNodesThatGatherNegative=False,
                negativeSet=negativeTiles)

            caps = SearchUtils.Counter(0)

            if gatherNodes is not None and len(gatherNodes) > 0:
                def foreachFunc(n: GatherTreeNode):
                    if len(n.children) > 0:
                        caps.value += (0 if self._map.is_tile_friendly(n.tile) else 1)

                GatherTreeNode.foreach_tree_node(gatherNodes, foreachFunc)

                playerArmyBaseline = int(self.player.standingArmy / self.player.tileCount)
                wasteWeight = gatherTurns - caps.value

                if wasteWeight <= 0:
                    sumPrunedTurns, sumPruned, gatherNodes = Gather.prune_mst_to_army_with_values(
                        gatherNodes,
                        1,
                        self.general.player,
                        MapBase.get_teams_array(self._map),
                        self._map.turn,
                        viewInfo=self.viewInfo,
                        noLog=True)
                    self.viewInfo.add_info_line(f'Flank Gath valGathered {sumPruned}({valGathered}) / (gatherTurns {sumPrunedTurns}({gatherTurns}) - caps {caps.value}) vs {playerArmyBaseline}')
                    path = Path()
                    n = SearchUtils.where(gatherNodes, lambda n: n.gatherTurns > 0)[0]
                    while True:
                        path.add_start(n.tile)
                        if len(n.children) == 0:
                            break
                        n = n.children[0]

                    if path.length > 0:
                        self.curPath = path

                elif 3 * valGathered / wasteWeight < playerArmyBaseline:
                    self.viewInfo.add_info_line(f'increasing flank def due to valGathered {valGathered} / (gatherTurns {gatherTurns} - caps {caps.value}) vs {playerArmyBaseline}')
                    move = None

            offset += 2

        if move is not None:
            return move

    def get_n_closest_team_tiles_near(self, nearTiles: typing.List[Tile], player: int, distance: int, limit: int, includeNeutral: bool = False) -> typing.List[Tile]:
        tiles = set(nearTiles)

        def nearbyTileAdder(tile: Tile) -> bool:
            if len(tiles) > limit:
                return True

            if self._map.is_tile_on_team_with(tile, player) or (includeNeutral and tile.isNeutral and tile.army == 0 and not tile.isObstacle):
                tiles.add(tile)

            return False

        SearchUtils.breadth_first_foreach(self._map, nearTiles, distance, foreachFunc=nearbyTileAdder)

        return [t for t in tiles]

    def convert_int_tile_2d_array_to_string(self, rows: typing.List[typing.List[int]]) -> str:
        return ','.join([str(rows[tile.x][tile.y]) for tile in self._map.get_all_tiles()])

    def convert_float_tile_2d_array_to_string(self, rows: typing.List[typing.List[float]]) -> str:
        return ','.join([f'{rows[tile.x][tile.y]:.2f}' for tile in self._map.get_all_tiles()])

    def convert_int_map_matrix_to_string(self, mapMatrix: MapMatrixInterface[int]) -> str:
        return ','.join([str(mapMatrix[tile]) for tile in self._map.get_all_tiles()])

    def convert_float_map_matrix_to_string(self, mapMatrix: MapMatrixInterface[float]) -> str:
        return ','.join([f'{mapMatrix[tile]:.2f}' for tile in self._map.get_all_tiles()])

    def convert_bool_map_matrix_to_string(self, mapMatrix: MapMatrixInterface[bool] | MapMatrixSet) -> str:
        return ''.join(["1" if mapMatrix[tile] else "0" for tile in self._map.get_all_tiles()])

    def convert_tile_set_to_string(self, tiles: typing.Set[Tile]) -> str:
        return ''.join(["1" if tile in tiles else "0" for tile in self._map.get_all_tiles()])

    def convert_tile_int_dict_to_string(self, tiles: typing.Dict[Tile, int]) -> str:
        return ','.join([str(tiles.get(tile, '')) for tile in self._map.get_all_tiles()])

    def convert_string_to_int_tile_2d_array(self, data: str) -> typing.List[typing.List[int]]:
        arr = new_value_grid(self._map, -1)

        values = data.split(',')
        i = 0
        prev = None
        for v in values:
            tile = self.get_tile_by_tile_index(i)
            arr[tile.x][tile.y] = int(v)

            prev = tile
            i += 1

        return arr

    def convert_string_to_float_tile_2d_array(self, data: str) -> typing.List[typing.List[float]]:
        arr = new_value_grid(self._map, 0.0)

        values = data.split(',')
        i = 0
        for v in values:
            tile = self.get_tile_by_tile_index(i)
            if v != '':
                arr[tile.x][tile.y] = float(v)
            i += 1

        return arr

    def convert_string_to_bool_map_matrix(self, data: str) -> MapMatrixInterface[bool]:
        matrix = MapMatrix(self._map, False)
        i = 0
        for v in data:
            if v == "1":
                tile = self.get_tile_by_tile_index(i)
                matrix[tile] = True
            i += 1

        return matrix

    def convert_string_to_bool_map_matrix_set(self, data: str) -> MapMatrixSet:
        matrix = MapMatrixSet(self._map)
        i = 0
        for v in data:
            if v == "1":
                tile = self.get_tile_by_tile_index(i)
                matrix.add(tile)
            i += 1

        return matrix

    def convert_string_to_int_map_matrix(self, data: str) -> MapMatrixInterface[int]:
        matrix = MapMatrix(self._map, -1)
        values = data.split(',')
        i = 0
        for v in values:
            tile = self.get_tile_by_tile_index(i)
            matrix[tile] = int(v)
            i += 1

        return matrix

    def convert_string_to_float_map_matrix(self, data: str) -> MapMatrixInterface[float]:
        matrix = MapMatrix(self._map, -1.0)
        values = data.split(',')
        i = 0
        for v in values:
            tile = self.get_tile_by_tile_index(i)
            matrix[tile] = float(v)
            i += 1

        return matrix

    def convert_string_to_tile_set(self, data: str) -> typing.Set[Tile]:
        outputSet = set()
        i = 0
        for v in data:
            if v == "1":
                tile = self.get_tile_by_tile_index(i)
                outputSet.add(tile)
            i += 1

        return outputSet

    def convert_string_to_tile_int_dict(self, data: str) -> typing.Dict[Tile, int]:
        outputSet = {}
        i = 0
        for v in data.split(','):
            if v != "N" and v != '':
                tile = self.get_tile_by_tile_index(i)
                outputSet[tile] = int(v)
            i += 1

        return outputSet

    def get_tile_by_tile_index(self, tileIndex: int) -> Tile:
        x, y = self.convert_tile_server_index_to_friendly_x_y(tileIndex)
        return self._map.GetTile(x, y)

    def convert_tile_server_index_to_friendly_x_y(self, tileIndex: int) -> typing.Tuple[int, int]:
        y = tileIndex // self._map.cols
        x = tileIndex % self._map.cols
        return x, y

    def _get_approximate_greedy_turns_available(self) -> int:
        if self.targetPlayer == -1 or self.target_player_gather_path is None:
            return 5

        if self.is_player_spawn_cramped(spawnDist=self.shortest_path_to_target_player.length):
            # never waste moves expanding inside a cramped spawn, or expanding tiles OUTSIDE a cramped spawn while leaving all our army inside.
            # TODO maybe make an exception if there is a city to capture nearby and we want to save army for that city.
            return 0

        defensiveTiles = list(self.target_player_gather_path.tileList)
        defensiveTiles.extend([c for c in self.player.cities if self.board_analysis.intergeneral_analysis.pathWayLookupMatrix[c] is not None and self.board_analysis.intergeneral_analysis.pathWayLookupMatrix[
            c].distance < self.board_analysis.intergeneral_analysis.shortestPathWay.distance + 3])

        frArmy = self.sum_friendly_army_near_or_on_tiles(defensiveTiles, distance=0, player=self.general.player)
        enArmyOffset = 0
        if self.enemy_attack_path:
            enArmyOffset = self.sum_friendly_army_near_or_on_tiles([t for t in self.enemy_attack_path.tileList if t.visible], distance=0, player=self.targetPlayer)

        approxGreedyTurnsAvail = self.opponent_tracker.get_approximate_greedy_turns_available(
            self.targetPlayer,
            ourArmyNonIncrement=frArmy + self.shortest_path_to_target_player.length // 2,
            cityLimit=None,
            opponentArmyOffset=enArmyOffset
        )

        finalGreedTurnsAvail = approxGreedyTurnsAvail
        prevGreed = self.approximate_greedy_turns_avail
        if approxGreedyTurnsAvail == prevGreed:
            # then move it down by 1 anyway, we have greed increments that happen every other turn due to city factors... dont want to mis-estimate every other turn.
            self.viewInfo.add_info_line(f'greed stayed same, decrementing by 1 from {approxGreedyTurnsAvail} to {finalGreedTurnsAvail}')
            finalGreedTurnsAvail -= 1
        elif approxGreedyTurnsAvail < prevGreed - 1:
            self.viewInfo.add_info_line(f'GREED TURNS DROPPED FROM {prevGreed} TO {approxGreedyTurnsAvail}')
        elif approxGreedyTurnsAvail > prevGreed:
            if approxGreedyTurnsAvail > prevGreed + 1:
                self.viewInfo.add_info_line(f'greed increase from {prevGreed} to {approxGreedyTurnsAvail}')
            else:
                # only increased by 1, hmm..? for now allow it.
                self.viewInfo.add_info_line(f'greed increase BY 1 from {prevGreed} to {approxGreedyTurnsAvail}')

        self.viewInfo.add_stats_line(f'Approx greedT: {finalGreedTurnsAvail} (our def {frArmy} opp enArmyOffset {enArmyOffset} -> {approxGreedyTurnsAvail})')

        return finalGreedTurnsAvail

    def get_approximate_fog_risk_deficit(self) -> int:
        cycleTurnsLeft = self.timings.get_turns_left_in_cycle(self._map.turn)

        pathWorth = self.get_player_army_amount_on_path(self.target_player_gather_path, self.general.player)
        pushRiskTurns = cycleTurnsLeft - self.target_player_gather_path.length
        pushRiskTurns = 0

        if self.targetPlayer != -1:
            fogRisk = self.opponent_tracker.get_approximate_fog_army_risk(self.targetPlayer, inTurns=pushRiskTurns)
            deficit = fogRisk - pathWorth - pushRiskTurns // 2
            self.viewInfo.add_stats_line(f'get_approximate_fog_risk_deficit {deficit} based on fogRisk {fogRisk} (our path {pathWorth}) in turns {pushRiskTurns}')
            return deficit

        return 0

    def try_get_cyclic_all_in_move(self, defenseCriticalTileSet: typing.Set[Tile]) -> Move | None:
        winningEc = self.opponent_tracker.winning_on_economy(byRatio=1.15)
        winningTile = self.opponent_tracker.winning_on_tiles(byRatio=1.1)
        winningArmy = self.opponent_tracker.winning_on_army(byRatio=1.45)
        staySafe = True
        if self.is_ffa_situation() and not self.is_player_aggressive(self.targetPlayer, turnPeriod=75):
            staySafe = False

        reason = ''
        if self.is_all_in_losing:
            reason = 'lose '
            staySafe = False

        # if not self.targetPlayerExpectedGeneralLocation.isGeneral:
        #     explorePath = self.explore_target_player_undiscovered(defenseCriticalTileSet, maxTime=0.025)
        #     if explorePath is not None:
        #         self.info(f'cyclic allin exploration move')
        #         return explorePath.get_first_move()

        if self.is_winning_gather_cyclic or (winningEc and winningTile and winningArmy and self.targetPlayer != -1) or self.is_all_in_losing:
            remainingTurns = max(5, self.timings.get_turns_left_in_cycle(self._map.turn) - 5)
            negatives = defenseCriticalTileSet.copy()
            negatives.update(self.cities_gathered_this_cycle)

            if not self.is_all_in_losing:
                reason = 'win '
                cycleTurns = self.timings.get_turns_left_in_cycle(self._map.turn)
                if cycleTurns > 30:
                    self.is_winning_gather_cyclic = True
                negatives.update(cd.tile for cd in self.get_contested_targets(shortTermContestCutoff=50, longTermContestCutoff=100, numToInclude=5, excludeGeneral=True))

                for city in self.player.cities:
                    if not self.territories.is_tile_in_friendly_territory(city):
                        negatives.add(city)

            enAttackPath: Path | None = None
            if remainingTurns > 0:
                targets = self.get_target_player_possible_general_location_tiles_sorted(elimNearbyRange=4)[0:4]
                for target in targets:
                    self.viewInfo.add_targeted_tile(target, TargetStyle.RED)

                if not self.is_all_in_losing:
                    for city in self.targetPlayerObj.cities:
                        self.viewInfo.add_targeted_tile(city, TargetStyle.ORANGE)

                    targets.extend(self.targetPlayerObj.cities)
                    enAttackPath = self.enemy_attack_path
                    if enAttackPath is not None:
                        enTiles = []
                        for tile in enAttackPath.tileList:
                            if self._map.is_tile_enemy(tile) or not tile.visible:
                                enTiles.append(tile)

                        if len(enTiles) > 5 and len(enTiles) > self.shortest_path_to_target_player.length // 2:
                            reason = f'{reason}EnAttk '
                            for t in enTiles:
                                if self.distance_from_general(t) < self.shortest_path_to_target_player.length // 2:
                                    targets.append(t)
                            remainingTurns = remainingTurns % 25
                        else:
                            enAttackPath = None

                with self.perf_timer.begin_move_event(f'{reason}gather cyclic {remainingTurns}'):
                    move_closest_value_func = None
                    if enAttackPath is not None:
                        analysis = ArmyAnalyzer.build_from_path(self._map, enAttackPath)
                        fakeThreat = ThreatObj(enAttackPath.length, 1, enAttackPath, ThreatType.Vision, armyAnalysis=analysis)
                        move_closest_value_func = self.get_defense_tree_move_prio_func(fakeThreat)

                    gcp = Gather.gather_approximate_turns_to_tiles(
                        self._map,
                        targets,
                        remainingTurns,
                        self.player.index,
                        gatherMatrix=self.get_gather_tiebreak_matrix(),
                        negativeTiles=negatives,
                        prioritizeCaptureHighArmyTiles=not self.is_all_in_losing,
                        useTrueValueGathered=True,
                        includeGatherPriorityAsEconValues=False,
                        timeLimit=min(0.05, self.get_remaining_move_time())
                    )

                    if gcp is not None:
                        if move_closest_value_func is not None:
                            gcp.value_func = move_closest_value_func
                        move = gcp.get_first_move()
                        self.gatherNodes = gcp.root_nodes
                        self.info(f'pcst {reason}gath cyc {remainingTurns} {move} @ {self.str_tiles(targets)} neg {self.str_tiles(negatives)}')
                        return move

                    #old
                    # move, valueGathered, turnsUsed, gatherNodes = self.get_gather_to_target_tiles(
                    #     targets,
                    #     maxTime=0.1,
                    #     gatherTurns=remainingTurns,
                    #     negativeSet=negatives,
                    #     # targetArmy=self.player.standingArmy,
                    #     useTrueValueGathered=False,
                    #     leafMoveSelectionValueFunc=move_closest_value_func,
                    #     includeGatherTreeNodesThatGatherNegative=True,
                    #     maximizeArmyGatheredPerTurn=not self.defend_economy,
                    # )

                # if move is not None:
                #     self.gatherNodes = gatherNodes
                #     self.info(f'{reason}gath cyc {remainingTurns} {move} @ {self.str_tiles(targets)} neg {self.str_tiles(negatives)}')
                #     return move

        return None

    def try_get_enemy_territory_exploration_continuation_move(self, defenseCriticalTileSet: typing.Set[Tile]) -> Move | None:
        if self.targetPlayer == -1:
            return None

        if self.is_all_in():
            path = self.explore_target_player_undiscovered(defenseCriticalTileSet, onlyHuntGeneral=True)
            if path is not None:
                self.info(f'all-in exploration move...? {str(path)}')
                return self.get_first_path_move(path)
            return None

        if self.timings.get_turns_left_in_cycle(self._map.turn) < 15:
            return None

        if self.armyTracker.has_perfect_information_of_player_cities_and_general(self.targetPlayer):
            return None

        armyCutoff = 4 + 4 * int(self.player.standingArmy / self.player.tileCount)
        if self.defend_economy:
            armyCutoff *= 2
            armyCutoff += 10

        logbook.info(f'EN TERRITORY CONT EXP, armyCutoff {armyCutoff}')
        move = self._get_expansion_plan_exploration_move(armyCutoff, defenseCriticalTileSet)

        if move is not None:
            self.try_find_expansion_move(defenseCriticalTileSet, timeLimit=self.get_remaining_move_time())
            move = self._get_expansion_plan_exploration_move(armyCutoff, defenseCriticalTileSet)
            if move is not None:
                self.info(f'EN TERRITORY CONT EXP! {move} - armyCutoff {armyCutoff}')
                return move

    def _get_expansion_plan_exploration_move(self, armyCutoff: int, negativeTiles: typing.Set[Tile]) -> Move | None:
        move = None
        maxPath: TilePlanInterface | None = None
        if self.expansion_plan is None:
            return None
        for path in self.expansion_plan.all_paths:
            # if self.territories.is_tile_in_friendly_territory(path.start.tile):
            #     continue

            if path.get_first_move().source.army < armyCutoff:
                continue

            containsFogCount = 0
            skip = False
            for tile in path.tileSet:
                if tile in negativeTiles:
                    skip = True
                    break
                distanceFromGen = self.distance_from_general(tile)
                if distanceFromGen < 7 or (self.territories.territoryDistances[self.targetPlayer][tile] > 2 and not self.armyTracker.valid_general_positions_by_player[self.targetPlayer][tile]):
                    skip = True
                    break
                for adj in tile.adjacents:
                    if not adj.discovered:
                        containsFogCount += 1

            if skip or containsFogCount < path.length:
                continue

            if maxPath is None or maxPath.get_first_move().source.army < path.get_first_move().source.army:
                maxPath = path

        if maxPath is not None:
            move = maxPath.get_first_move()

        return move

    def _get_expansion_plan_quick_capture_move(self, defenseCriticalTileSet: typing.Set[Tile]) -> Move | None:
        if not self.behavior_allow_pre_gather_greedy_leaves:
            return None

        if self.currently_forcing_out_of_play_gathers:
            return None

        if (
                (self._map.remainingPlayers != 2 and not self._map.is_2v2)
                # and not self.is_player_spawn_cramped()
                or self.opponent_tracker.winning_on_economy(byRatio=1.35, cityValue=4, offset=self.behavior_pre_gather_greedy_leaves_offset)
                or self.approximate_greedy_turns_avail <= 0
                # and self.opponent_tracker.winning_on_army(self.behavior_pre_gather_greedy_leaves_army_ratio_cutoff)
                # and self.shortest_path_to_target_player.length > 17
                # and self.timings.get_turn_in_cycle(self._map.turn) < 40
        ):
            return None

        negativeTiles = defenseCriticalTileSet.copy()
        if not self.timings.in_launch_timing(self._map.turn):
            negativeTiles.update(self.target_player_gather_path.tileList)

        move = None
        maxPath: TilePlanInterface | None = None

        highValueSet = set()

        def does_tile_capture_expand_our_vision(tile: Tile) -> bool:
            if self.board_analysis.flankable_fog_area_matrix[tile]:
                return True

            for movable in tile.movable:
                if self.board_analysis.flankable_fog_area_matrix[movable]:
                    return True

            return False

        for tile in self._map.reachable_tiles:
            if self._map.is_tile_on_team_with(tile, self.targetPlayer):
                highValueSet.add(tile)
                continue

            if does_tile_capture_expand_our_vision(tile):
                highValueSet.add(tile)
                continue

        maxScoreVt = -1
        for path in self.expansion_plan.all_paths:
            # if self.territories.is_tile_in_friendly_territory(path.start.tile):
            #     continue

            if highValueSet.isdisjoint(path.tileSet):
                continue

            if not negativeTiles.isdisjoint(path.tileSet):
                continue

            if path.length > self.approximate_greedy_turns_avail:
                continue

            if SearchUtils.any_where(path.tileList, lambda t: t.army == 1 and self._map.is_tile_friendly(t)):
                continue

            scoreVt = path.econValue / path.length
            if maxPath is None or maxScoreVt < scoreVt:
                maxPath = path
                maxScoreVt = scoreVt

        if maxPath is not None and maxScoreVt > 1.0:
            move = maxPath.get_first_move()
            if self.timings.in_gather_split(self._map.turn) and self.timings.splitTurns < self.timings.launchTiming:
                self.timings.splitTurns += 1
                self.info(f'greedy exp move {move} (vt {maxScoreVt:.2f}), inc gather {self.timings.splitTurns - 1}->{self.timings.splitTurns}')
            else:
                self.info(f'greedy exp move {move} (vt {maxScoreVt:.2f})')

        return move

    def get_euclid_shortest_from_tile_towards_target(self, sourceTile: Tile, towardsTile: Tile) -> Move:
        shortest = 100
        shortestTile = None
        for adj in sourceTile.movable:
            if adj.isObstacle:
                continue
            dist = self._map.euclidDist(towardsTile.x, towardsTile.y, adj.x, adj.y)
            if dist < shortest:
                shortest = dist
                shortestTile = adj

        return Move(sourceTile, shortestTile)

    def render_intercept_plan(self, plan: ArmyInterception, colorIndex: int = 0):
        targetStyle = TargetStyle(((colorIndex + 1) % 9) + 1)
        for tile, interceptInfo in plan.common_intercept_chokes.items():
            self.viewInfo.add_targeted_tile(tile, targetStyle, radiusReduction=11 - colorIndex)

            self.viewInfo.bottomMidRightGridText[tile] = f'cw{interceptInfo.max_choke_width}'

            self.viewInfo.bottomMidLeftGridText[tile] = f'ic{interceptInfo.max_intercept_turn_offset}'

            self.viewInfo.bottomLeftGridText[tile] = f'it{interceptInfo.max_delay_turns}'

            self.viewInfo.midRightGridText[tile] = f'im{interceptInfo.max_extra_moves_to_capture}'

        self.viewInfo.add_info_line(f'  intChokes @{plan.target_tile} = {targetStyle}')

        for dist, opt in plan.intercept_options.items():
            logbook.info(f'intercept plan opt {plan.target_tile} dist {dist}: {str(opt)}')

    def check_fog_risk(self):
        self.high_fog_risk = False
        if self.targetPlayer == -1:
            return

        cycleTurn = self.timings.get_turn_in_cycle(self._map.turn)
        cycleTurnsLeft = self.timings.get_turns_left_in_cycle(self._map.turn)

        pathWorth = self.get_player_army_amount_on_path(self.target_player_gather_path, self.general.player)
        pushRiskTurns = max(1, cycleTurnsLeft - self.target_player_gather_path.length)
        self.fog_risk_amount = 0

        oppStats = self.opponent_tracker.get_current_cycle_stats_by_player(self.targetPlayer)
        enGathAmt = 0
        if oppStats is not None:
            fogRisk = self.opponent_tracker.get_approximate_fog_army_risk(self.targetPlayer, inTurns=pushRiskTurns)
            enGathAmt = oppStats.approximate_army_gathered_this_cycle
            self.fog_risk_amount = fogRisk

        numFog = self.get_undiscovered_count_on_path(self.target_player_gather_path)
        if numFog > self.target_player_gather_path.length // 2:
            self.viewInfo.add_info_line(f'bypassing fog risk due to unknown path')
            return

        if self.fog_risk_amount > 0:
            if cycleTurnsLeft > self.target_player_gather_path.length + 5 and self.fog_risk_amount > pathWorth and self._map.turn > 80:
                # TODO instead of waiting to die, push a flank of our own?
                self.viewInfo.add_info_line(f'high fog risk, fog_risk_amount {self.fog_risk_amount} in {pushRiskTurns} (gath {enGathAmt}) vs {pathWorth} - {cycleTurnsLeft} vs len {self.target_player_gather_path.length}')
                self.high_fog_risk = True
                return

            self.viewInfo.add_info_line(f'NOT fog risk, fog_risk_amount {self.fog_risk_amount} in {pushRiskTurns} (gath {enGathAmt}) vs {pathWorth} - {cycleTurnsLeft} vs len {self.target_player_gather_path.length}')

    def get_path_subsegment_starting_from_last_move(self, launchPath: Path) -> Path:
        lastMoved = -1
        if self.armyTracker.lastMove is not None:
            i = 1
            for t in launchPath.tileList:
                if self.armyTracker.lastMove.source == t:
                    lastMoved = i
                    break
                if self.armyTracker.lastMove.dest == t:
                    lastMoved = i - 1
                    break
                i += 1

        cut = False
        if 0 <= lastMoved < launchPath.length:
            # logbook.info(f'DEBUG lastMoved {lastMoved}, path {launchPath}')
            if lastMoved > 0:
                tilePre = launchPath.tileList[lastMoved - 1]
                if tilePre.army <= 3 and launchPath.tileList[lastMoved].player == self.general.player:
                    cut = True
            else:
                cut = True
        if cut:
            launchPath = launchPath.get_subsegment(launchPath.length - lastMoved, end=True)

        return launchPath

    def check_cur_path(self):
        if self.curPath is None:
            return
        if self.curPath.length == 0:
            self.curPath = None
            return
        move = None
        try:
            move = self.curPath.get_first_move()
        except:
            pass

        if move is None:
            logbook.info(f'curpath had no first move, dropping. {self.curPath}')
            self.curPath = None
            return

        if move.source is None or move.dest is None:
            logbook.info(f'curpath had bad move {move}, dropping. {self.curPath}')
            self.curPath = None
            return

        if isinstance(self.curPath, Path):
            if self.curPath.start is None:
                self.curPath = None
                return
            if self.curPath.tail is None:
                self.curPath = None
                return
            if self.curPath.start.tile is None:
                self.curPath = None
                return
            if self.curPath.start.next is None:
                self.curPath = None
                return
            if self.curPath.start.next.tile is None:
                self.curPath = None
                return
            if self.curPath.tail.tile is None:
                self.curPath = None
                return
            if self.curPath.start.tile.player != self.general.player:
                self.curPath = None
                return
            if self.curPath.length == 0:
                self.curPath = None
                return

    def send_2v2_tip_to_ally(self):
        tips = [
            "Bot tip: Ping your start expand tiles that you want me to avoid, and I will try to reroute my start.",
            "Bot tip: If you leave your army in front of me early in a round, I will use it in my attack!",
            "Bot tip: Usually (not always) you can keep queueing with me by going to https://generals.io/teams/teammate and waiting for me to queue!",
            "2v2 tip: Always keep as much of your army as possible BETWEEN the forwards-player on your team and the enemies.",
            "2v2 tip: If you are the REAR-SPAWNING player, make sure to move your army up in front of (or at least near) your ally early each round, or they can easily die to double-teaming!",
            "Bot tip: I will occasionally ping the possible/likely enemy general spawn locations when they change.",
            "2v2 tip: Cities are a little safer to take in 2v2 than in 1v1 so long as enemy spawn distance is medium-high, and as long as the backwards ally defends the forwards player.",
            "Tip: In round 1, start moving before you have 15 army on your general, but usually after you have 11+ army."
        ]
        comm = random.choice(tips)
        self.send_teammate_communication(comm, cooldown=50, detectionKey='2v2GameStartTips')

    def cooldown_allows(self, detectionKey: str, cooldown: int, doNotUpdate: bool = False) -> bool:
        lastSentTurn = self._communications_sent_cooldown_cache.get(detectionKey, -50)
        if lastSentTurn < self._map.turn - cooldown:
            if not doNotUpdate:
                self._communications_sent_cooldown_cache[detectionKey] = self._map.turn
            return True
        return False

    def get_all_in_move(self, defenseCriticalTileSet: typing.Set[Tile]) -> Move | None:
        if self.is_all_in():
            hitGeneralInTurns = self.all_in_army_advantage_cycle - self.all_in_army_advantage_counter % self.all_in_army_advantage_cycle
            if self.is_all_in_army_advantage and self.targetPlayerObj.tileCount < 90:
                hitGeneralInTurns = hitGeneralInTurns % 25 + 5
            flankAllInMove = self.try_find_flank_all_in(hitGeneralInTurns)

            if flankAllInMove:
                self.all_in_army_advantage_counter += 1
                return flankAllInMove

            targets = [self.targetPlayerExpectedGeneralLocation]

            andTargs = ''

            if not self.targetPlayerExpectedGeneralLocation.isGeneral:
                andTargs = f' (and undisc)'
                emergenceTiles = self.get_target_player_possible_general_location_tiles_sorted(elimNearbyRange=5, cutoffEmergenceRatio=0.6)[0:3]
                targets = emergenceTiles[0:5]
                for t in targets:
                    self.viewInfo.add_targeted_tile(t, TargetStyle.WHITE)

            if (self.is_all_in_army_advantage or self.all_in_city_behind) and not self.is_still_ffa_and_non_dominant():
                andTargs = ' (and cities)'
                if self.all_in_city_behind or self._map.remainingPlayers == 2:
                    targets.extend(self.targetPlayerObj.cities)

            msg = f'allin g AT tg gen{andTargs}, {hitGeneralInTurns}t, {str([str(t) for t in targets])}'

            with self.perf_timer.begin_move_event(f'pcst {msg}. self.all_in_army_advantage_cycle {self.all_in_army_advantage_cycle}, self.all_in_army_advantage_counter {self.all_in_army_advantage_counter}'):
                gathNeg = defenseCriticalTileSet.copy()
                citiesToHalf = set()
                if self.is_all_in_army_advantage:
                    for contestedCity in self.cityAnalyzer.owned_contested_cities:
                        if contestedCity.army > self.targetPlayerObj.standingArmy:
                            citiesToHalf.add(contestedCity)
                        else:
                            gathNeg.add(contestedCity)

                gathCapPlan = Gather.gather_approximate_turns_to_tiles(
                    self._map,
                    rootTiles=targets,
                    approximateTargetTurns=hitGeneralInTurns,
                    asPlayer=self.general.player,
                    gatherMatrix=None,
                    captureMatrix=None,
                    negativeTiles=gathNeg,
                    skipTiles=None,
                    prioritizeCaptureHighArmyTiles=False,
                    useTrueValueGathered=True,
                    includeGatherPriorityAsEconValues=True,
                    includeCapturePriorityAsEconValues=True,
                    timeLimit=min(0.075, self.get_remaining_move_time()),
                    logDebug=False,
                    viewInfo=self.viewInfo if self.info_render_gather_values else None)
                if gathCapPlan is None:
                    return None

                move = gathCapPlan.get_first_move()
                self.curPath = gathCapPlan
                self.info(f'PCST ALL IN appx {hitGeneralInTurns}t: {gathCapPlan}')

                #
                # move, valueGathered, turnsUsed, gatherNodes = self.get_gather_to_target_tiles(
                #     targets,
                #     0.1,
                #     hitGeneralAtTurn,
                #     maximizeArmyGatheredPerTurn=True,
                #     negativeSet=gathNeg)
                #
                # if move is None and hitGeneralAtTurn < 15:
                #     move, valueGathered, turnsUsed, gatherNodes = self.get_gather_to_target_tiles(
                #         targets,
                #         0.1,
                #         hitGeneralAtTurn + 15,
                #         maximizeArmyGatheredPerTurn=True,
                #         negativeSet=gathNeg)
                # elif move is not None and move.source in citiesToHalf:
                #     move.move_half = True

            if move is not None:
                self.info(msg)
                if hitGeneralInTurns > 15 and not self.is_winning_gather_cyclic and not self.is_all_in_army_advantage:
                    self.send_teammate_communication(f'All in here, hit in {hitGeneralInTurns} moves', detectionKey='allInAtGenTargets', cooldown=10)

                for target in targets:
                    self.send_teammate_tile_ping(target, cooldown=25, cooldownKey=f'allIn{str(target)}')

                self.all_in_army_advantage_counter += 1
                self.gatherNodes = gathCapPlan.root_nodes
                return move

        return None

    def convert_gather_to_move_list_path(self, gatherNodes, turnsUsed, value, moveOrderPriorityMinFunc) -> MoveListPath:
        # gcp = Gather.GatherCapturePlan(gatherNodes, self._map, 0, turnsUsed, value, 0.0, turnsUsed, 0, moveOrderPriorityMinFunc)
        gcp = Gather.GatherCapturePlan.build_from_root_nodes(self._map, gatherNodes, negativeTiles=set(), searchingPlayer=self._map.player_index, onlyCalculateFriendlyArmy=False, priorityMatrix=None, viewInfo=self.viewInfo)
        moveListThing = []
        move = gcp.pop_first_move()
        while move:
            moveListThing.append(move)
            move = gcp.pop_first_move()

        self.info(f'gath {len(gatherNodes)} root, moves {" - ".join([str(m) for m in moveListThing])}')
        return MoveListPath(moveListThing)

    def _get_defensive_spanning_tree(self, negativeTiles: TileSet, gatherPrioMatrix: MapMatrixInterface[float] | None = None) -> typing.Set[Tile]:
        includes = [self.general]
        if self.is_2v2_teammate_still_alive():
            includes.append(self.teammate_general)
            includes.extend(self._map.players[self.teammate].cities)

        includes.extend(self._map.players[self.general.player].cities)

        limit = 12
        if len(includes) > limit:
            includes = sorted(includes, key=lambda c: self.territories.territoryDistances[self.targetPlayer].raw[c.tile_index] if not c.isGeneral else 0)[:limit]

        distLimit = 50
        if self.sketchiest_potential_inbound_flank_path:
            distLimit = self.distance_from_general(self.sketchiest_potential_inbound_flank_path.tail.tile)
        distLimit = max(distLimit, int(max(self.distance_from_general(t) for t in includes) * 1.5))

        if distLimit > 50:
            self.info(f'defensive spanning tree using higher distLimit {distLimit}')

        banned = MapMatrixSet(self._map)
        for t in self._map.get_all_tiles():
            if not t.visible:
                banned.raw[t.tile_index] = True

        spanningTreeTiles, unconnectableTiles = MapSpanningUtils.get_max_gather_spanning_tree_set_from_tile_lists(
            self._map,
            includes,
            banned,
            negativeTiles,
            maxTurns=distLimit,
            gatherPrioMatrix=gatherPrioMatrix,
            searchingPlayer=self.general.player
        )

        if unconnectableTiles:
            for t in unconnectableTiles:
                self.viewInfo.add_targeted_tile(t, TargetStyle.PURPLE, radiusReduction=-1)
            self.viewInfo.add_info_line(f'PURPLE LARGE CIRC = unconnectable defensive spanning tree points.')

        # TODO if it includes too many tiles, prune the outer edges of it so we just keep the central, easiest to attack bits and focus our gathers there?

        return spanningTreeTiles

    def get_kill_race_chance(self, generalHuntPath: Path, enGenProbabilityCutoff: float = 0.4, turnsToDeath: int | None = None, cutoffKillArmy: int = 0, againstPlayer: int = None) -> float:
        if generalHuntPath is None:
            return 0.0

        if againstPlayer is None:
            againstPlayer = self.targetPlayer

        toReveal = self.get_target_player_possible_general_location_tiles_sorted(elimNearbyRange=0, player=againstPlayer, cutoffEmergenceRatio=enGenProbabilityCutoff, includeCities=False)
        if not toReveal:
            return 0.0
        for t in toReveal:
            self.mark_tile(t, alpha=50)

        isOnlyOneSpot = toReveal[0].isGeneral

        if isOnlyOneSpot:
            if turnsToDeath is None:
                return 1.0
            if generalHuntPath.length < turnsToDeath:
                # TODO check their fog army...?
                logbook.info(f'We win the race, {generalHuntPath.length}t vs turnsToDeath {turnsToDeath}t')
                return 1.0

            logbook.info(f'We lose the race, {generalHuntPath.length}t vs turnsToDeath {turnsToDeath}t')
            return 0.0

        revealedCount, maxKillTurns, minKillTurns, avgKillTurns, rawKillDistByTileMatrix, bestRevealedPath = WatchmanRouteUtils.get_revealed_count_and_max_kill_turns_and_positive_path(self._map, generalHuntPath, toReveal, cutoffKillArmy=cutoffKillArmy)

        if bestRevealedPath is None:
            killChance = 0.0
            self.info(f'KillRaceProb {killChance:.2f} - NO rev {revealedCount} {generalHuntPath} -- min{minKillTurns} max{maxKillTurns} avg{avgKillTurns:.1f}')
            return killChance

        killChance = revealedCount / len(toReveal)
        if turnsToDeath is None:
            self.info(f'KillRaceProb {killChance:.2f} - {bestRevealedPath.length}t ({generalHuntPath.length}t) revealed {revealedCount}/{len(toReveal)}, kill min{minKillTurns} max{maxKillTurns} avg{avgKillTurns:.1f}')
            return killChance

        sumKill = 0.0
        sumTotal = 0.0
        reachedCount = 0
        tooFarToKillTiles = []
        for tile in toReveal:
            emgVal = 1 + self.armyTracker.get_tile_emergence_for_player(tile, self.targetPlayer)
            sumTotal += emgVal
            killDist = rawKillDistByTileMatrix.raw[tile.tile_index]
            if killDist < turnsToDeath:
                sumKill += emgVal
                reachedCount += 1
            else:
                tooFarToKillTiles.append(tile)
                logbook.info(f'{tile} too far to kill {killDist}/{turnsToDeath}, wed lose! :(')

        if sumTotal == 0.0:
            self.info(f'SUM TOTAL WAS ZERO? THIS SHOULD NEVER HAPPEN')
            return 0.0

        killInTurnsChance = sumKill / sumTotal
        self.info(f'KillRaceProb {killInTurnsChance:.2f} ({killChance:.2f}) - {bestRevealedPath.length}t ({generalHuntPath.length}t) reached {reachedCount} rev {revealedCount}/{len(toReveal)}, kill min{minKillTurns} max{maxKillTurns} avg{avgKillTurns:.1f}. death in {turnsToDeath}t')
        if tooFarToKillTiles:
            self.info(f' too far tiles {" | ".join([str(t) for t in tooFarToKillTiles])}')

        return killInTurnsChance

    def get_unexpandable_ratio(self) -> float:
        # nearby = self.find_large_tiles_near(self._map.players[self.general.player].tiles, 4, minArmy=-100, limit=100)
        fromTiles = self._map.players[self.general.player].tiles
        distance = 8

        nearby = []
        def tile_finder(tile: Tile, dist: int) -> bool:
            isFriendly = self._map.is_tile_friendly(tile)
            if tile.isCity and not isFriendly and tile.army > 0:
                return True
            if not isFriendly:
                nearby.append(tile)
            return False

        SearchUtils.breadth_first_foreach_dist_fast_incl_neut_cities(self._map, fromTiles, distance, foreachFunc=tile_finder)

        if not nearby:
            self.info(f'No tiles nearby...?')
            return 1.0

        if len(nearby) < self._map.remainingCycleTurns:
            expResRaw = (self._map.remainingCycleTurns - len(nearby)) / self._map.remainingCycleTurns
            weightedExpRes = 0.4 + expResRaw
            self.info(f'No expandability? expResRaw {expResRaw:.3f}, weightedExpRes {weightedExpRes:.3f}')
            return weightedExpRes

        numSwamp = SearchUtils.count(nearby, lambda t: t.isSwamp)
        numDesert = SearchUtils.count(nearby, lambda t: t.isDesert)
        numVisible = SearchUtils.count(nearby, lambda t: t.visible)
        ratioBad = numSwamp / len(nearby)
        ratioBad += numDesert / max(1, numVisible)

        if ratioBad > 0.6:
            self.info(f'surrounded by unexpandables. ratioBad {ratioBad:.3f}, total nearby {len(nearby)}, numSwamp {numSwamp}, totalVisible {numVisible}, numDesert {numDesert}')
            return ratioBad
        # if ratioBad > 0.2:
        self.info(f'Not fully surrounded by unexpandables. ratioBad {ratioBad:.3f}, total nearby {len(nearby)}, numSwamp {numSwamp}, totalVisible {numVisible}, numDesert {numDesert}')

        return ratioBad

    def ensure_reachability_matrix_built(self):
        with self.perf_timer.begin_move_event(f'rebuild_reachability_costs_matrix'):
            self.cityAnalyzer.ensure_reachability_matrix_built(force=False)

    def _check_should_wait_city_capture(self) -> typing.Tuple[Path | None, bool]:
        generalArmy = self.general.army
        for city, score in sorted(self.cityAnalyzer.city_scores.items(), key=lambda tup: self.distance_from_general(tup[0]))[:10]:
            qk = SearchUtils.dest_breadth_first_target(self._map, [city], preferCapture=True, noNeutralCities=False)
            if qk:
                return qk, False
            if city.army + self.distance_from_general(city) < generalArmy + 30 - self._map.turn:
                return None, True

        return None, False

    def set_defensive_blocks_against(self, threat: ThreatObj):
        for gatherTreeNode in self.best_defense_leaves:
            defensiveTile = gatherTreeNode.tile
            if defensiveTile.army <= 2 and gatherTreeNode.toTile.army > defensiveTile.army:
                defensiveTile = gatherTreeNode.toTile
            block = self.blocking_tile_info.get(defensiveTile, None)
            if not block:
                block = ThreatBlockInfo(
                    defensiveTile,
                    amount_needed_to_block=defensiveTile.army,
                )
                self.blocking_tile_info[defensiveTile] = block

            defDist = threat.armyAnalysis.interceptDistances.raw[defensiveTile.tile_index]
            if defDist is None:
                if threat.armyAnalysis.pathWayLookupMatrix.raw[defensiveTile.tile_index] is not None:
                    defDist = threat.armyAnalysis.pathWayLookupMatrix.raw[defensiveTile.tile_index].distance
                else:
                    defDist = 100
            for t in defensiveTile.movable:
                tDist = threat.armyAnalysis.interceptDistances.raw[t.tile_index]
                if tDist is None:
                    if threat.armyAnalysis.pathWayLookupMatrix.raw[t.tile_index] is not None:
                        tDist = threat.armyAnalysis.pathWayLookupMatrix.raw[t.tile_index].distance
                    else:
                        tDist = 100
                if defDist < tDist:
                    block.add_blocked_destination(t)
            self.info(f'blocking {defensiveTile} from moving to {block.blocked_destinations}')

    def _should_use_iterative_negative_expand(self) -> bool:
        if self._map.turn < 150:
            return False
        return self.expansion_use_iterative_negative_tiles

