from __future__ import annotations

import itertools
import typing

import logbook

import SearchUtils
from ArmyAnalyzer import ArmyAnalyzer
from BoardAnalyzer import BoardAnalyzer
from DangerAnalyzer import ThreatObj, ThreatType
from Models import Move
from Interfaces import TilePlanInterface, MapMatrixInterface
from Path import Path
from base.client.map import MapBase, Tile

# TODO remove me once things fixed
DEBUG_BYPASS_BAD_INTERCEPTIONS = True

TARGET_CAP_VALUE = 2.21
OTHER_PARTY_CAP_VALUE = 0.5
NEUTRAL_CAP_VALUE = 1.0
GENERAL_CAP_VALUE = 25.0
TARGET_CITY_FLAT_BONUS = 5
# needs to be high enough to outperform normal expand, or normal expand will try to skip the intercept in favor of dodging the army for captures lmao
RECAPTURE_VALUE = 2.21


class ThreatValueInfo(object):
    __slots__ = (
        'threat',
        'econ_value',
        'turns_used_by_enemy',
        'econ_value_per_turn',
    )

    def __init__(self, threat: ThreatObj, econValue: float, turns: int):
        self.threat: ThreatObj = threat
        self.econ_value: float = econValue
        self.turns_used_by_enemy: int = turns
        self.econ_value_per_turn: float = econValue / max(1, turns)

    def __str__(self):
        return f'Threat {self.threat.path.start.tile}-->{self.threat.path.tail.tile} (econ {self.econ_value:.2f}, turns {self.turns_used_by_enemy}, vt {self.econ_value_per_turn:.2f}) {self.threat}'

    def __repr__(self):
        return str(self)


class ThreatBlockInfo(object):
    __slots__ = (
        'tile',
        'amount_needed_to_block',
        'blocked_destinations',
    )

    def __init__(self, tile: Tile, amount_needed_to_block: int):
        self.tile: Tile = tile
        self.amount_needed_to_block: int = amount_needed_to_block
        # list, not set, because set is slower to check than list for small numbers of entries, and we can never have more than 4
        self.blocked_destinations: typing.List[Tile] = []

    def add_blocked_destination(self, tile: Tile):
        if tile not in self.blocked_destinations:
            self.blocked_destinations.append(tile)

    def __str__(self) -> str:
        return f'{str(self.tile)}:{str(self.amount_needed_to_block)}@{"|".join([str(t) for t in self.blocked_destinations])}'


class InterceptPointTileInfo(object):
    __slots__ = (
        'tile',
        'max_delay_turns',
        'max_extra_moves_to_capture',
        'max_choke_width',
        'max_intercept_turn_offset',
        'max_search_dist',
    )

    def __init__(self, tile: Tile, maxDelayTurns: int, maxExtraMoves: int, maxChokeWidth: int, maxInterceptTurnOffset: int):
        self.tile: Tile = tile

        self.max_delay_turns: int = maxDelayTurns
        """The latest turns in the future that this tile must be reached in order to prevent subsequent damage in the worst case scenarios."""

        self.max_extra_moves_to_capture: int = maxExtraMoves
        """The maximum worst case number of moves that will be wasted to complete an intercept capture from this tile if reached by the min_delay_turns turn."""

        self.max_choke_width: int = maxChokeWidth
        """The width of the max choke. Informational only?"""

        self.max_intercept_turn_offset: int = maxInterceptTurnOffset
        """Mostly useless? The max offset based on chokewidths and distances to chokes, used to calculate the max_delay_turns"""

        self.max_search_dist: int = 30
        """Used to limit time spent searching mediocre intercept points."""

    def __str__(self) -> str:
        return f'{self.tile} - it{self.max_delay_turns}, cw{self.max_choke_width}, ic{self.max_intercept_turn_offset}, im{self.max_extra_moves_to_capture}'

    def __repr__(self) -> str:
        return str(self)


class InterceptionOptionInfo(TilePlanInterface):
    __slots__ = (
        'path',
        '_econ_value',
        '_turns',
        'damage_blocked',
        'intercepting_army_remaining',
        'recapture_turns',
        'best_case_intercept_moves',
        'worst_case_intercept_moves',
        '_requiredDelay',
        'friendly_army_reaching_intercept',
        'intercept',
    )

    def __init__(
            self,
            path: Path,
            econValue: float,
            turns: int,
            damageBlocked: float,
            interceptingArmyRemaining: int,
            bestCaseInterceptMoves: int,
            worstCaseInterceptMoves: int,
            recaptureTurns: int,
            requiredDelay: int,
            friendlyArmyReachingIntercept: int,
    ):
        self.path: Path = path
        if path is None or path.start is None:
            raise Exception(f"Wtf, dont pass interception a bad path :V {path}")

        self._econ_value: float = econValue
        self._turns: int = turns
        self.damage_blocked: float = damageBlocked
        """The economic damage prevented by this intercept."""
        self.intercepting_army_remaining: int = interceptingArmyRemaining
        """The amount of the attacking army that will likely be remaining after the intercept completes AT the intercept point."""

        self.recapture_turns: int = recaptureTurns
        """Number of turns AVAILABLE to recapture at round end that have NOT been included already in the option, based on army left over at intercept."""

        self.best_case_intercept_moves: int = bestCaseInterceptMoves
        """Best case turns-to-intercept, if they walk right towards our army."""

        self.worst_case_intercept_moves: int = worstCaseInterceptMoves
        """Worst case turns-to-intercept"""

        self._requiredDelay: int = requiredDelay

        self.friendly_army_reaching_intercept: int = friendlyArmyReachingIntercept

        self.intercept: ArmyInterception | None = None

    @property
    def post_intercept_implied_moves(self) -> int:
        """
        Number of moves we expect to spend recapturing after the intercept path completes.

        Note 1 move may be wasted chasing if the intercept happens adjacently on a non-priority move, so factor in that this could be over-shooting the number of available recapture turns by 1.
        """
        return self._turns - self.path.length

    @property
    def length(self) -> int:
        return max(self._turns, self.path.length + self.recapture_turns, self.best_case_intercept_moves)

    @property
    def turns(self) -> int:
        """The number of actual moves planned here (for example maybe 1 move to block a choke against an army that is 10 moves away from that choke)."""
        return self._turns

    @property
    def econValue(self) -> float:
        """
        Includes the econ value of the recapture PLUS the econ value of the opponents that is blocked. So this is the net-sum of true econ capture value plus the econ damage blocked.

        @return:
        """
        return self._econ_value

    @econValue.setter
    def econValue(self, value: float):
        self._econ_value = value

    @property
    def tileSet(self) -> typing.Set[Tile]:
        return self.path.tileSet

    @property
    def tileList(self) -> typing.List[Tile]:
        return self.path.tileList

    @property
    def requiredDelay(self) -> int:
        return self._requiredDelay

    def get_move_list(self) -> typing.List[Move]:
        return self.path.get_move_list()

    def get_first_move(self) -> Move:
        return self.path.get_first_move()

    def pop_first_move(self) -> Move:
        return self.path.pop_first_move()

    def __str__(self):
        # if self.path is not None and self.path.start is not None:
        tgTile = 'NONE'
        if self.intercept is not None:
            tgTile = str(self.intercept.target_tile)
        return f'int {self.path.start.tile}@{tgTile}->{self.path.tail.tile}: {self.econValue:.2f}v/{self._turns}t ({self._econ_value / max(1, self._turns):.2f}vt), re {self.recapture_turns}, dBlk {self.damage_blocked:.2f}, eRem {self.intercepting_army_remaining}, bct {self.best_case_intercept_moves}, wct {self.worst_case_intercept_moves}, del{self.requiredDelay}'
        # else:
        #     return f'int ?@{self.intercept.target_tile}->?: {self.econValue:.2f}v/{self._turns}t ({self._econ_value / max(1, self._turns):.2f}vt) dBlk {self.damage_blocked:.2f}, eRem {self.intercepting_army_remaining}, bct {self.best_case_intercept_moves}, del{self.requiredDelay}'

    def __repr__(self):
        return f'{str(self)}, path {self.path}'

    def clone(self) -> InterceptionOptionInfo:
        clone = InterceptionOptionInfo(
            self.path.clone(),
            self.econValue,
            self._turns,
            self.damage_blocked,
            self.intercepting_army_remaining,
            self.best_case_intercept_moves,
            self.worst_case_intercept_moves,
            self.recapture_turns,
            self._requiredDelay,
            self.friendly_army_reaching_intercept)
        return clone


class ArmyInterception(object):
    __slots__ = (
        'threat_values',
        'threats',
        'ignored_threats',
        'base_threat_army',
        'common_intercept_chokes',
        'furthest_common_intercept_distances',
        'target_tile',
        'kill_enemy_threat',
        'best_enemy_threat',
        'intercept_options',
    )

    def __init__(
        self,
        threats: typing.List[ThreatValueInfo],
        ignoredThreats: typing.List[ThreatObj]
    ):
        self.threat_values: typing.List[ThreatValueInfo] = threats
        self.threats: typing.List[ThreatObj] = [t.threat for t in threats]

        self.ignored_threats: typing.List[ThreatObj] = ignoredThreats

        self.base_threat_army: int = 0
        self.common_intercept_chokes: typing.Dict[Tile, InterceptPointTileInfo] = {}
        self.furthest_common_intercept_distances: MapMatrixInterface[int] = None
        self.target_tile: Tile = threats[0].threat.path.start.tile

        maxValPerTurn = -100000
        maxThreatInfo = None
        isMaxThreatKill = False

        killThreats = []
        # check kill threats first
        for threatInfo in threats:
            if threatInfo.threat.threatType != ThreatType.Kill or threatInfo.threat.threatValue < 0:
                continue

            killThreats.append(threatInfo)

            val = threatInfo.econ_value
            valPerTurn = threatInfo.econ_value_per_turn
            if valPerTurn == maxValPerTurn and threatInfo.threat.path.length < maxThreatInfo.threat.path.length:
                maxValPerTurn -= 1

            if valPerTurn > maxValPerTurn:
                maxValPerTurn = valPerTurn
                maxThreatInfo = threatInfo
                isMaxThreatKill = True

        self.kill_enemy_threat: ThreatValueInfo = maxThreatInfo
        """The highest econ kill threat, should one exist."""

        # check non-kill threats second
        for threatInfo in threats:
            if threatInfo in killThreats:
                continue

            val = threatInfo.econ_value
            valPerTurn = threatInfo.econ_value_per_turn

            if valPerTurn > maxValPerTurn or (valPerTurn == maxValPerTurn and threatInfo.threat.path.length > maxThreatInfo.threat.path.length and not isMaxThreatKill):
                if isMaxThreatKill:
                    logbook.warn(f'WARNING: replacing best_enemy_threat kill threat with non-kill:\r\nreplacing: {maxThreatInfo.threat}\r\nwith: {threatInfo.threat}')
                isMaxThreatKill = False
                maxValPerTurn = valPerTurn
                maxThreatInfo = threatInfo

        self.best_enemy_threat: ThreatValueInfo = maxThreatInfo
        """The highest econ value per turn threat. MAY NOT BE A KILL THREAT WHILE A KILL THREAT EXISTS."""
        if self.kill_enemy_threat is None:
            self.kill_enemy_threat = self.best_enemy_threat

        self.intercept_options: typing.Dict[int, InterceptionOptionInfo] = {}
        """turnsToIntercept -> econValueOfIntercept, interceptPath"""

    def get_intercept_plan_values_by_path(self, path: Path) -> typing.Tuple[int | None, float | None]:
        """returns distance, value"""
        for dist, optionInfo in self.intercept_options.items():
            val = optionInfo.econValue
            p = optionInfo.path
            if p.start.tile == path.start.tile and p.tail.tile == path.tail.tile:
                return dist, val

        return None, None

    def get_intercept_option_by_path(self, path: Path | TilePlanInterface) -> InterceptionOptionInfo | None:
        """returns distance, value"""
        if isinstance(path, InterceptionOptionInfo):
            return path

        if not isinstance(path, Path):
            return None

        for dist, optionInfo in self.intercept_options.items():
            p = optionInfo.path
            if p.start.tile == path.start.tile and p.tail.tile == path.tail.tile:
                return optionInfo

        return None


class ArmyInterceptor(object):
    __slots__ = (
        'map',
        'board_analysis',
        'log_debug',
    )

    def __init__(
        self,
        map: MapBase,
        boardAnalysis: BoardAnalyzer,
        useDebugLogging: bool = False
    ):
        self.map: MapBase = map
        self.board_analysis: BoardAnalyzer = boardAnalysis
        self.log_debug: bool = useDebugLogging

    def get_interception_plan(
        self,
        threats: typing.List[ThreatObj],
        turnsLeftInCycle: int,
        otherThreatsBlockingTiles: typing.Dict[Tile, ThreatBlockInfo] | None = None
    ) -> ArmyInterception | None:
        threatValues, ignoredThreats = self._prune_threats_to_valuable_threat_info(threats, turnsLeftInCycle)

        if len(threatValues) == 0:
            return None

        interception = ArmyInterception(threatValues, ignoredThreats)

        interception.common_intercept_chokes = self.get_shared_chokes(interception.threat_values, interception)
        threatMovable = [t for t in interception.target_tile.movableNoObstacles]
        if len(interception.common_intercept_chokes) <= len(threatMovable):
            countMightBeMovable = len(threatMovable) - len(interception.common_intercept_chokes)
            for mv in threatMovable:
                if mv in interception.common_intercept_chokes:
                    countMightBeMovable -= 1

            if countMightBeMovable == 0:
                # TODO do something with this info...?
                logbook.warn(f'ALL of the {len(interception.common_intercept_chokes)} common intercept chokes were only one tile adjacent to the threat. Should probably re-evaluate which threats we care about, then...?')

        interception.base_threat_army = self._get_threats_army_amount(threats)
        # potentialRecaptureArmyInterceptTable = self._get_potential_intercept_table(turnsLeftInCycle, interception.base_threat_army)
        interception.intercept_options = self._get_intercept_plan_options(interception, turnsLeftInCycle, otherThreatsBlockingTiles)
        if len(interception.intercept_options) == 0:
            logbook.warn(f'No intercept options found, retrying shared chokes but being more lenient filtering out threats')
            # try again, more friendly
            altThreatValues = [t for t in threatValues if t.threat.path.get_first_move().dest != t.threat.path.start.tile.delta.fromTile]
            if len(threatValues) > len(altThreatValues) > 0:
                newIgnored = [t.threat for t in threatValues if t not in altThreatValues]
                newIgnored.extend(ignoredThreats)
                interception = ArmyInterception(altThreatValues, newIgnored)

                interception.common_intercept_chokes = self.get_shared_chokes(interception.threat_values, interception)

                interception.base_threat_army = self._get_threats_army_amount(interception.threats)

                interception.intercept_options = self._get_intercept_plan_options(interception, turnsLeftInCycle, otherThreatsBlockingTiles)

        return interception

    def get_shared_chokes(
            self,
            threats: typing.List[ThreatValueInfo],
            interceptData: ArmyInterception,
    ) -> typing.Dict[Tile, InterceptPointTileInfo]:
        commonChokesCounts: typing.Dict[Tile, int] = {}
        # commonChokesCombinedTurnOffsets: typing.Dict[Tile, int] = {}
        commonMinDelayTurns = {}
        commonMaxExtraMoves = {}

        isThreatNotMoving = threats[0].threat.path.start.tile.lastMovedTurn < self.map.turn - 1
        # additionalOffset = 0
        # if isThreatNotMoving:
        #     additionalOffset = 2

        # withinOneAdditionalChecks = {}
        for threatValueInfo in threats:
            threat = threatValueInfo.threat
            intChokes = threat.armyAnalysis.interceptChokes
            intTurnsMat = threat.armyAnalysis.interceptTurns
            intDistMat = threat.armyAnalysis.interceptDistances
            # withinOneAdditionalChecks.clear()

            def foreachFunc(tile):
                interceptMoves = intChokes.raw[tile.tile_index]
                if interceptMoves is None:
                    return

                delayTurns = intTurnsMat.raw[tile.tile_index]
                if delayTurns > 800 or delayTurns > threat.turns + 1:
                    return

                worstCaseExtraMoves = intDistMat.raw[tile.tile_index]

                curCount = commonChokesCounts.get(tile, 0)
                curExtraMoves = commonMaxExtraMoves.get(tile, 0)
                curMinDelayTurns = commonMinDelayTurns.get(tile, 1000)
                commonChokesCounts[tile] = curCount + 1
                # commonChokesCombinedTurnOffsets[tile] = curVal + interceptMoves + 1
                commonMaxExtraMoves[tile] = max(worstCaseExtraMoves, curExtraMoves)
                commonMinDelayTurns[tile] = min(curMinDelayTurns, delayTurns)

            SearchUtils.breadth_first_foreach_fast_no_neut_cities(self.map, threat.armyAnalysis.shortestPathWay.tiles, maxDepth=1, foreachFunc=foreachFunc)
            #     for movable in tile.movable:
            #         if movable in threat.armyAnalysis.interceptChokes or movable.isObstacle:
            #             continue
            #         if threat.armyAnalysis.bMap[movable] >= threat.turns + 1 or threat.armyAnalysis.aMap[movable] > threat.turns + 1:
            #             continue
            #         existingMin = withinOneAdditionalChecks.get(movable, 10000)
            #         if existingMin > interceptMoves:
            #             withinOneAdditionalChecks[movable] = interceptMoves
            #
            # for tile, interceptMoves in withinOneAdditionalChecks.items():
            #     curCount = commonChokesCounts.get(tile, 0)
            #     curVal = commonChokesVals.get(tile, 0)
            #     commonChokesCounts[tile] = curCount + 1
            #     commonChokesVals[tile] = curVal + interceptMoves + 1

        sharedThreshold = 0
        validChokeOptCount = 0
        validThresh = min(len(threats) - 1, 1)
        skips = {t.tile_index for t in itertools.chain.from_iterable(mv.movable for mv in threats[0].threat.path.start.tile.movable)}
        for tile, num in commonChokesCounts.items():
            if tile.tile_index in skips:
                # we dont consider the start tile or its immediate neighbors a shared choke from a 'find max shared count' perspective
                # TODO change if we start intercepting agnostic of the maximal shared chokes..?
                continue
            if num > validThresh:
                validChokeOptCount += 1
            if num > sharedThreshold:
                sharedThreshold = num

        countMaxShared = 0
        minChokesThresh = validChokeOptCount // 3
        while True:
            for tile, num in commonChokesCounts.items():
                if tile.tile_index in skips:
                    # we dont consider the start tile or its immediate neighbors a shared choke from a 'find max shared count' perspective
                    # TODO change if we start intercepting agnostic of the maximal shared chokes..?
                    continue
                if num >= sharedThreshold:
                    countMaxShared += 1
            if countMaxShared > minChokesThresh:
                break

            if sharedThreshold <= 0:
                break

            logbook.info(f'Not enough shared chokes, {countMaxShared} < minChokesThresh {minChokesThresh}, making less restrictive {sharedThreshold}')
            sharedThreshold -= 1
            countMaxShared = 0

        potentialSharedChokes = set()
        for tile, num in commonChokesCounts.items():
            if num < sharedThreshold:
                continue

            potentialSharedChokes.add(tile)
            # maxWidth = -1
            # for threat in threats:
            #     interceptMoves = threat.armyAnalysis.interceptChokes.get(tile, -1)
            #     if interceptMoves == -1:
            #         continue
            #
            #     if interceptMoves > maxWidth:
            #         maxWidth = interceptMoves
            # if maxWidth >= 0:
            #     potentialSharedChokes[tile] = maxWidth

        if self.log_debug:
            # for tile, chokeVal in sorted(commonChokesCombinedTurnOffsets.items()):
            #     logbook.info(f'chokeVals: {str(tile)} = count {commonChokesCounts[tile]} - chokeVal {chokeVal}')

            for tile in potentialSharedChokes:
                dist = commonMinDelayTurns[tile]
                logbook.info(f'potential shared: {str(tile)} = dist {dist}')

        sharedChokes = self._build_shared_chokes(
            potentialSharedChokes,
            commonMaxExtraMoves,
            commonMinDelayTurns,
            threats)

        # if self.log_debug:
        #     for tile, dist in sorted(sharedChokes.items()):
        #         logbook.info(f'potential shared: {str(tile)} = dist {dist}')

        indexesToKeepIfBad = 1
        if len(sharedChokes) == 1 and len(threats) > indexesToKeepIfBad and threats[0].threat.path.start.tile in sharedChokes:
            logbook.info(f'No shared chokes found against {threats}, falling back to just first threat intercept...')
            interceptData.ignored_threats.extend([t.threat for t in threats[indexesToKeepIfBad:]])
            return self.get_shared_chokes(threats[0:indexesToKeepIfBad], interceptData)

        return sharedChokes

    def _build_shared_chokes(
            self,
            potentialSharedChokes: typing.Set[Tile],
            commonMaxExtraMoves: typing.Dict[Tile, int],
            commonMinDelayTurns: typing.Dict[Tile, int],
            threats: typing.List[ThreatValueInfo]
    ) -> typing.Dict[Tile, InterceptPointTileInfo]:
        sharedChokes = {}
        genBlockTile = None
        threatenedGen = None
        for threatInfo in threats:
            threat = threatInfo.threat
            if not threat.path.tail.tile.isGeneral or not self.map.is_tile_friendly(threat.path.tail.tile):
                continue

            if threat.threatValue < 0:
                # TODO probably this needs to take into account the true value of the threat path...? PotentialThreat excludes our large blocking tiles, right?
                continue

            threatenedGen = threat.path.tail.tile
            # if threat.saveTile is not None:
            #     genBlockTile = threat.saveTile
            #     # isGenThreatWithMultiPath = False
            #     continue
            # isGenThreatWithMultiPath = True
            for tile in threat.path.tileList[-2:1]:
                if tile.isGeneral:
                    continue
                # ito = threat.armyAnalysis.interceptChokes[tile]
                cw = threat.armyAnalysis.chokeWidths[tile]
                # dists = threat.armyAnalysis.tileDistancesLookup[tile]
                # dist = threat.armyAnalysis.interceptDistances[tile]
                if cw == 1:
                    # isGenThreatWithMultiPath = False
                    genBlockTile = tile
                    break

            if genBlockTile is not None:
                break

            if threat.saveTile is not None:
                genBlockTile = threat.saveTile
                break

        blockDist = 1000
        if threatenedGen:
            blockDist = 0
            if genBlockTile:
                blockDist = self.map.distance_mapper.get_distance_between(threatenedGen, genBlockTile)

        for tile in potentialSharedChokes:
            maxDelayTurns = commonMinDelayTurns[tile]
            maxExtraMoves = commonMaxExtraMoves[tile]

            maxChokeWidth = 0
            maxInterceptTurnOffset = 0
            unsafeGenLateIntercept = False
            for threatInfo in threats:
                threat = threatInfo.threat
                isGenThreat = threat.path.tail.tile.isGeneral
                ito = threat.armyAnalysis.interceptChokes[tile]
                cw = threat.armyAnalysis.chokeWidths[tile]
                if cw is not None and (not threatenedGen or isGenThreat):
                    maxChokeWidth = max(cw, maxChokeWidth)
                if ito is not None:
                    maxInterceptTurnOffset = max(ito, maxInterceptTurnOffset)

                # if isGenThreat:
                #     tileDist = threat.armyAnalysis.aMap.raw[tile.tile_index]
                #     if tileDist < 2:
                #         enMoves = threat.armyAnalysis.aMap.raw[threat.path.start.tile.tile_index] - tileDist
                #         if not self.map.player_has_priority_over_other(self.map.player_index, threat.threatPlayer, self.map.turn + enMoves):
                #             unsafeGenLateIntercept = True

            # if maxChokeWidth == 1 and not unsafeGenLateIntercept:
            #     maxDelayTurns += 1

            if threatenedGen is not None and blockDist < self.map.distance_mapper.get_distance_between(threatenedGen, tile) and tile != threats[0].threat.path.start.tile:
                if self.log_debug:
                    logbook.info(f'INCREASED MAX INTERCEPT {tile} DUE PRE CHOKE')
                # maxInterceptTurnOffset += 1
                maxDelayTurns -= 1
            if self.log_debug:
                logbook.info(f'common choke {str(tile)} was maxDelayTurns {maxDelayTurns}, maxExtraMoves {maxExtraMoves}, maxChokeWidth {maxChokeWidth}, maxInterceptTurnOffset {maxInterceptTurnOffset}')
            sharedChokes[tile] = InterceptPointTileInfo(tile, maxDelayTurns, maxExtraMoves, maxChokeWidth, maxInterceptTurnOffset)

        return sharedChokes

    def _get_threats_army_amount(self, threats: typing.List[ThreatObj]) -> int:
        maxAmount = 0
        for threat in threats:
            curAmount = 0
            curTurn = self.map.turn + 1
            curOffset = 1
            for tile in threat.path.tileList:
                if self.map.is_tile_on_team_with(tile, threat.threatPlayer):
                    curAmount += tile.army - curOffset
                else:
                    if tile.army < threat.threatValue // 5:
                        curAmount -= tile.army + curOffset

                if curTurn % 50 == 0:
                    curOffset += 1

                curTurn += 1

            if curAmount > maxAmount:
                maxAmount = curAmount

        return maxAmount

    def _get_potential_intercept_table(self, turnsLeftInCycle: int, baseThreatArmy: int) -> typing.List[float]:
        """
        Returns a turn-offset lookup table of how much army we would want to intercept with at any given turn for a max recapture.
        We never need MORE army than this, but gathering 1 extra turn to move one up the intercept table is good ish.
        """

        potentialRecaptureArmyInterceptTable = [
            baseThreatArmy + 1.8 * i for i in range(turnsLeftInCycle)
        ]

        if turnsLeftInCycle < 15:
            for i in range(10):
                potentialRecaptureArmyInterceptTable.append(baseThreatArmy + 3 * i)

        if self.log_debug:
            for i, val in enumerate(potentialRecaptureArmyInterceptTable):
                logbook.info(f'intercept value turn {self.map.turn + i} = {val}')

        return potentialRecaptureArmyInterceptTable

    def _prune_threats_to_valuable_threat_info(self, threats: typing.List[ThreatObj], turnsLeftInCycle: int) -> typing.Tuple[typing.List[ThreatValueInfo], typing.List[ThreatObj]]:
        """
        Returns threatsToConsider, threatsIgnored

        @param threats:
        @param turnsLeftInCycle:
        @return:
        """
        if len(threats) == 0:
            raise AssertionError(f'Threat list was empty.')

        outThreats = []
        ignoredThreats: typing.List[ThreatObj] = []

        threatTile = threats[0].path.start.tile
        countCity = 0
        countGen = 0
        countExpansion = 0
        for threat in threats:
            if threat.path.length <= 0:
                ignoredThreats.append(threat)
                continue
            # Why was this here?
            # if threat.turns > 30:
            #     logbook.info(f'skipping long threat len {threat.turns} from {str(threat.path.start.tile)} to {str(threat.path.tail.tile)}')
            #     continue

            if threat.path.tail.tile.isGeneral:
                countGen += 1
                if countGen > 2:
                    logbook.info(f'bypassing {countGen}+ general threat {threat.path}')
                    ignoredThreats.append(threat)
                    continue
            elif threat.path.tail.tile.isCity:
                countCity += 1
                if countCity > 2:
                    logbook.info(f'bypassing {countCity}+ city threat {threat.path}')
                    ignoredThreats.append(threat)
                    continue
            else:
                countExpansion += 1
                if countExpansion > 3:
                    logbook.info(f'bypassing {countExpansion}+ expansion threat {threat.path}')
                    ignoredThreats.append(threat)
                    continue

            self.ensure_threat_army_analysis(threat)
            if threat.path.start.tile != threatTile:
                raise AssertionError(f'Can only get an interception plan for threats from one tile at a time. {str(threat.path.start.tile)} vs {str(threatTile)}')
            outThreats.append(threat)

        threatValues = self._determine_threat_values(outThreats, turnsLeftInCycle)
        maxLen = 0
        avgLen = 0
        avgEconPerTurn = 0.0
        maxEconPerTurn = 0.0
        for threat in threatValues:
            maxLen = max(threat.turns_used_by_enemy, maxLen)
            avgLen += threat.turns_used_by_enemy
            avgEconPerTurn += threat.econ_value_per_turn
            maxEconPerTurn = max(maxEconPerTurn, threat.econ_value_per_turn)
        avgLen = avgLen / len(threatValues)
        avgEconPerTurn = avgEconPerTurn / len(threatValues)

        lenCutoffIfNotCompliant = max(maxLen // 4, int(2 * avgLen / 3))
        lenCutoffIfNotCompliant = min(turnsLeftInCycle - 2, lenCutoffIfNotCompliant)

        # econVtCutoff = avgEconPerTurn * 0.8
        econVtCutoff = maxEconPerTurn * 0.85
        finalThreats = []
        for threat in threatValues:
            if not threat.threat.path.tail.tile.isGeneral and threat.turns_used_by_enemy <= lenCutoffIfNotCompliant:
                logbook.info(f'bypassing threat due to turns {threat.turns_used_by_enemy} vs cutoff {lenCutoffIfNotCompliant:.3f} based on average length {avgLen:.3f}. Cut {threat}')
                ignoredThreats.append(threat.threat)
                continue
            if not threat.threat.path.tail.tile.isGeneral and threat.econ_value_per_turn <= econVtCutoff:
                logbook.info(f'bypassing threat due to econVt {threat.econ_value_per_turn:.3f} vs cutoff {econVtCutoff:.3f}. Cut {threat}')
                ignoredThreats.append(threat.threat)
                continue

            logbook.info(f'Kept threat with {threat.econ_value_per_turn:.3f}vt vs cutoff {econVtCutoff:.3f}vt: (threat {threat})')
            finalThreats.append(threat)

        return finalThreats, ignoredThreats

    def ensure_threat_army_analysis(self, threat: ThreatObj) -> bool:
        """returns True if the army analysis was built"""
        if threat.path.value == 0:
            threat.path.calculate_value(threat.threatPlayer, self.map.team_ids_by_player_index)
        if threat.armyAnalysis is None:
            threat.armyAnalysis = ArmyAnalyzer.build_from_path(self.map, threat.path, bypassRetraverse=threat.threatType == ThreatType.Econ, maxDist=max(int(6 + threat.turns / 2), self.map.get_distance_between(threat.path.start.tile, threat.path.tail.tile)))
            return True
        return False

    def _determine_threat_values(self, threats: typing.List[ThreatObj], turnsLeftInCycle: int) -> typing.List[ThreatValueInfo]:
        maxValPerTurn = -100000

        maxThreatInfo = None
        maxThreatKills = False

        threatValues = []

        for threat in threats:
            enPlayer = threat.threatPlayer
            frPlayer = self.map.player_index
            val, threatLen = self._get_path_econ_values_for_player(threat.path, enPlayer, frPlayer, turnsLeftInCycle)
            if threat.path.tail.tile.isGeneral:
                # for THIS calculation, we undo this value. We'd rather assume they'll dodge our general unless they have a legit kill threat.
                val -= GENERAL_CAP_VALUE
            threatInfo = ThreatValueInfo(threat, val, threatLen)
            valPerTurn = threatInfo.econ_value_per_turn
            if valPerTurn == maxValPerTurn and threat.path.length > maxThreatInfo.threat.path.length:
                # the fuck is this code for?
                maxValPerTurn -= 0.0000001

            isKillThreat = threat.threatType == ThreatType.Kill and threat.threatValue > 0

            if (maxThreatKills == isKillThreat and valPerTurn > maxValPerTurn) or (maxThreatKills is False and isKillThreat):
                maxValPerTurn = valPerTurn
                maxThreatInfo = threatInfo
                maxThreatKills = isKillThreat

            threatValues.append(threatInfo)

        logbook.info(f'best_enemy_threat was val {maxThreatInfo.econ_value:.2f} v/t {maxThreatInfo.econ_value_per_turn:.2f} - {str(maxThreatInfo)}')
        return threatValues

    def _get_path_econ_values_for_player(
            self,
            interceptionPath: Path,
            searchingPlayer: int,
            targetPlayer: int,
            turnsLeftInCycle: int,
            interceptingArmy: int = 0,
            includeRecaptureEffectiveStartDist: int = -1
    ) -> typing.Tuple[float, int]:
        """
        Returns (value, turnsUsed).
        turnsUsed is always the path length unless includeRecaptureEffectiveStartDist >= 0.
        value includes the recaptured tile value for the turns used here.
        """
        val = 0
        cityCaps = 0
        genCaps = 0
        curTurn = self.map.turn
        cycleEnd = self.map.turn + turnsLeftInCycle
        armyLeft = 0
        pathNode = interceptionPath.start
        cityHoldTurns = 0
        while pathNode is not None:
            tile = pathNode.tile
            if armyLeft <= 0 and curTurn > self.map.turn:
                curTurn += 1
                break

            if not self.map.is_tile_on_team_with(tile, searchingPlayer):
                armyLeft -= tile.army

                # no bonuses if the path is negative by this point.
                if armyLeft > 0:
                    isTarget = self.map.is_tile_on_team_with(tile, targetPlayer)
                    if tile.isGeneral:
                        genCaps += 1
                        if isTarget:
                            val += GENERAL_CAP_VALUE
                    if tile.isCity:
                        # cityCaps += 1
                        cityHoldTurns += cycleEnd - curTurn
                        if isTarget:
                            # cityCaps += 1
                            # get a bonus point for the target losing city, too. Double the reward.
                            val += TARGET_CITY_FLAT_BONUS
                            cityHoldTurns += cycleEnd - curTurn
                    if isTarget:
                        val += TARGET_CAP_VALUE
                    elif tile.player >= 0:
                        val += OTHER_PARTY_CAP_VALUE
                    else:
                        val += NEUTRAL_CAP_VALUE
            else:
                armyLeft += tile.army

                if pathNode.move_half:
                    # we still intend to use the rest of the split army to capture the rest of the round, so DONT exclude it from the recaptures.
                    # armyLeft = armyLeft - armyLeft // 2
                    # however DO penalize our turn count, as we use one extra turn to do this.
                    curTurn += 1

            armyLeft -= 1

            curTurn += 1

            if (curTurn & 1) == 0:
                val += cityCaps

            pathNode = pathNode.next

            if curTurn > cycleEnd:
                break

        # account for we considered the first tile in the list a move, when it is just the start tile
        curTurn -= 1
        armyLeft -= interceptingArmy

        if cycleEnd > curTurn and armyLeft > 0:
            left = cycleEnd - curTurn
            val += cityHoldTurns // 2

            if includeRecaptureEffectiveStartDist >= 0:
                left -= includeRecaptureEffectiveStartDist
                curTurn += includeRecaptureEffectiveStartDist
                recaps = max(0, min(left, armyLeft // 2))
                curTurn += recaps
                val += recaps * TARGET_CAP_VALUE  # have to keep this same as the factor in expansion algo, or we pick expansion over intercept...

        return val, curTurn - self.map.turn

    def _get_intercept_plan_options(
            self,
            interception: ArmyInterception,
            turnsLeftInCycle: int,
            otherThreatsBlockingTiles: typing.Dict[Tile, ThreatBlockInfo] | None = None
    ) -> typing.Dict[int, InterceptionOptionInfo]:
        """turnsToIntercept -> econValueOfIntercept, interceptPath"""

        if len(interception.common_intercept_chokes) == 0:
            return {}

        furthestBackCommonIntercept = max(interception.common_intercept_chokes.keys(), key=lambda t: interception.threats[0].armyAnalysis.bMap[t])
        interception.furthest_common_intercept_distances = self.map.distance_mapper.get_tile_dist_matrix(furthestBackCommonIntercept)
        threatDistFromCommon = interception.furthest_common_intercept_distances.raw[interception.target_tile.tile_index]
        longestThreat = max(interception.threats, key=lambda t: t.turns)
        maxDepth = longestThreat.turns + 1

        averageEnemyPositionByTurn: typing.List[typing.Tuple[float, float] | None] = [None] * max(interception.kill_enemy_threat.threat.path.length + 1, maxDepth)
        for i in range(0, interception.kill_enemy_threat.threat.path.length):
            numThreatsAtThisDist = 0
            allThreatsX = 0
            allThreatsY = 0
            for threat in interception.threat_values:
                mult = 1
                if threat.threat.threatType == ThreatType.Kill and threat.threat.path.tail.tile.isGeneral:
                    mult = 3
                distances = threat.threat.armyAnalysis.tileDistancesLookup.get(i, None)
                if not distances:
                    continue

                numThreatsAtThisDist += mult
                xSum = 0
                ySum = 0
                numTilesAtThisDist = 0
                for t in distances:
                    xSum += t.x
                    ySum += t.y
                    numTilesAtThisDist += 1

                xAvg = xSum / numTilesAtThisDist
                yAvg = ySum / numTilesAtThisDist
                allThreatsX += xAvg * mult
                allThreatsY += yAvg * mult

            if numThreatsAtThisDist == 0:
                continue

            avgAllThreatsX = allThreatsX / numThreatsAtThisDist
            avgAllThreatsY = allThreatsY / numThreatsAtThisDist
            averageEnemyPositionByTurn[i] = avgAllThreatsX, avgAllThreatsY
            logbook.info(f'avgPos dist {i} = {avgAllThreatsX:.1f},{avgAllThreatsY:.1f}')

        logbook.info(f'filtering out poor intercept points and setting search depths by intercept tile')
        self.filter_interception_best_points(interception, maxDepth, positionsByTurn=averageEnemyPositionByTurn)

        bestInterceptTable: typing.Dict[int, InterceptionOptionInfo] = {}

        logbook.info(f'getting intercept paths at maxDepth {maxDepth}, threatDistFromCommon {threatDistFromCommon}')

        tile: Tile
        interceptInfo: InterceptPointTileInfo
        for tile, interceptInfo in interception.common_intercept_chokes.items():
            if tile.isCity and tile.isNeutral:
                continue

            turnsToIntercept = interceptInfo.max_delay_turns
            depth = interceptInfo.max_search_dist
            if depth <= 0:
                if self.log_debug:
                    logbook.info(f'\r\n\r\nSkipping tile {str(tile)} with depth (max_search_dist) {depth}')
                continue

            if self.log_debug:
                logbook.info(f'\r\n\r\nChecking tile {str(tile)} with depth {depth}  -  threatDistFromCommon {threatDistFromCommon} / min(maxDepth={maxDepth}, turnsToIntercept={turnsToIntercept})')

            interceptPaths = self._get_intercept_paths(
                tile,
                interception,
                maxDepth=depth,
                turnsLeftInCycle=turnsLeftInCycle,
                threatDistFromCommon=threatDistFromCommon,
                searchingPlayer=self.map.player_index,
                positionsByTurn=averageEnemyPositionByTurn,
                otherThreatsBlockingTiles=otherThreatsBlockingTiles,
            )

            debugBadCutoff = max(1, interception.base_threat_army // 10)

            for dist, path in interceptPaths.items():
                interceptPointDist = interception.threats[0].armyAnalysis.bMap[path.tail.tile]
                """The distance from the threat to the intercept point"""

                if DEBUG_BYPASS_BAD_INTERCEPTIONS and path.start.tile.army <= debugBadCutoff:
                    logbook.error(f'bypassed bad intercept plan {str(path)}')
                    continue

                addlTurns = interceptPointDist
                """The number of additional turns beyond the path length that will need to be travelled to recoup our current tile-differential...? to reach the threat tile."""
                if not interception.target_tile.isCity:
                    # interceptRemaining = max(0, interceptPointDist - path.length)
                    # where the opponents army will ideally have moved to while we are moving to this position
                    # addlTurns = max(0, interceptRemaining - interceptRemaining // 2)
                    addlTurns = max(0, interceptPointDist - path.length)
                    # addlTurns = max(0, interceptRemaining // 2)

                # effectiveDist = path.length + addlTurns + interceptWorstCaseDistance
                effectiveDist = path.length + addlTurns + interceptInfo.max_extra_moves_to_capture
                """The effective distance that we need to travel before recapture starts"""

                interceptArmy = interception.base_threat_army
                if interception.target_tile in path.tileSet:
                    interceptArmy -= interception.target_tile.army - 1

                shouldDelay, shouldSplit = self._should_delay_or_split(tile, path, interception.threat_values, turnsLeftInCycle)
                splittingForSafetyOnSingleIntercept = shouldSplit
                """This will be true when we split just against this intercept (and thus intend to use both halves of this split army for the intercept recapture, still."""

                if shouldDelay and self.log_debug:
                    logbook.info(f'DETERMINED SHOULD DELAY FOR {tile} {path}')

                (
                    enInterceptPointTile,
                    blockedDamage,
                    enemyArmyLeftAfterIntercept,
                    enemyArmyCollidedWithAtIntercept,
                    bestCaseInterceptMoves,
                    worstCaseInterceptMoves
                ) = self._get_value_of_threat_blocked(interception, path, interception.best_enemy_threat, turnsLeftInCycle)

                if otherThreatsBlockingTiles is not None:
                    pathNode = path.start
                    split = False
                    while pathNode is not None:
                        t = pathNode.tile
                        tileHold = otherThreatsBlockingTiles.get(t, None)
                        if tileHold is not None and pathNode.next is not None and pathNode.next.tile in tileHold.blocked_destinations:
                            # TODO this is wrong, almost certainly prevents LEGITIMATE splits. This hack was added for  test_should_intercept_instead_of_eating_damage_on_late_attacks_after_defensive_gather_timing_increment and test_should_intercept_instead_of_eating_damage_on_late_attacks_after_defensive_gather_timing_increment (unit test)
                            if enemyArmyLeftAfterIntercept > 0:
                                if self.log_debug:
                                    logbook.info(f'forcing {pathNode.tile} move half due to blocked destinations in {path}')
                                pathNode.move_half = True
                                split = True
                        pathNode = pathNode.next
                    if split:
                        # recalculate
                        (
                            enInterceptPointTile,
                            blockedDamage,
                            enemyArmyLeftAfterIntercept,
                            enemyArmyCollidedWithAtIntercept,
                            bestCaseInterceptMoves,
                            worstCaseInterceptMoves
                        ) = self._get_value_of_threat_blocked(interception, path, interception.best_enemy_threat, turnsLeftInCycle)

                fullAddl = addlTurns
                if splittingForSafetyOnSingleIntercept:
                    fullAddl += 1

                # TODO this is returning extra moves, see test_should_full_intercept_all_options
                rawValue, turnsUsed = self._get_path_econ_values_for_player(
                    path,
                    searchingPlayer=self.map.player_index,
                    targetPlayer=interception.threats[0].threatPlayer,
                    turnsLeftInCycle=turnsLeftInCycle,
                    interceptingArmy=enemyArmyCollidedWithAtIntercept,
                    includeRecaptureEffectiveStartDist=fullAddl)  #+ (1 if shouldDelay else 0)
                newValue = rawValue

                # INTENTIONALLY DO THIS AFTER THE VALUE CALCULATION AND OTHER SPLIT CALCULATION ABOVE, BECAUSE WE DONT ACTUALLY INTEND TO RECAPTURE WITH ONLY THE SPLIT PART OF THE PATH, WE WILL PULL THE FULL ARMY STILL IF BETTER. THE SPLIT IS JUST TO GUARANTEE OUR SAFETY, SO ADD ONE EXTRA WASTED MOVE INSTEAD.
                if shouldSplit:
                    path.start.move_half = True

                # TODO use best/worst case intercept moves instead...? WHy do i calc those if we're not using them???
                turnsUsed += interceptInfo.max_extra_moves_to_capture

                newValue += blockedDamage

                if self.log_debug:
                    logbook.info(f'interceptPointDist:{interceptPointDist}, addlTurns:{fullAddl}, effectiveDist:{effectiveDist}, turnsUsed:{turnsUsed}, blockedAmount:{blockedDamage}, maxExtraMoves:{interceptInfo.max_extra_moves_to_capture}')

                # for curDist in range(path.length + addlTurns, turnsUsed + 1):
                for curDist in range(path.length, turnsUsed + 1):
                    recaptureTurns = turnsUsed - curDist - interceptInfo.max_extra_moves_to_capture  # interceptInfo.max_extra_moves_to_capture dont count as recapture turns
                    # thisValue = max(rawValue, newValue - recaptureTurns * RECAPTURE_VALUE)
                    thisValue = newValue - recaptureTurns * RECAPTURE_VALUE

                    existing = bestInterceptTable.get(curDist, None)
                    opt = InterceptionOptionInfo(
                        path,
                        thisValue,
                        curDist,
                        damageBlocked=blockedDamage,
                        interceptingArmyRemaining=enemyArmyLeftAfterIntercept,
                        bestCaseInterceptMoves=bestCaseInterceptMoves,
                        worstCaseInterceptMoves=worstCaseInterceptMoves,
                        recaptureTurns=recaptureTurns,
                        requiredDelay=1 if shouldDelay else 0,
                        friendlyArmyReachingIntercept=enemyArmyCollidedWithAtIntercept - enemyArmyLeftAfterIntercept)

                    opt.intercept = interception

                    if existing is None:
                        if self.log_debug:
                            logbook.info(f'setting bestInterceptTable[dist {curDist}]:\n  new  {str(opt)}')
                        bestInterceptTable[curDist] = opt
                        continue

                    if thisValue > existing.econValue:
                        if self.log_debug:
                            logbook.info(f'replacing bestInterceptTable[dist {curDist}]:\n  prev {str(existing)}\n  new  {str(opt)}')
                        bestInterceptTable[curDist] = opt

        if self.log_debug:
            for i in range(interception.threats[0].armyAnalysis.shortestPathWay.distance + 1):
                vals = bestInterceptTable.get(i, None)
                if vals:
                    logbook.info(f'best turns {i} = {str(vals)}')
                else:
                    logbook.info(f'best turns {i} = NONE')

        return bestInterceptTable

    def filter_interception_best_points(
            self,
            interception,
            maxDepth,
            positionsByTurn: typing.List[typing.Tuple[float, float]]
    ):
        # TODO sort by earliest intercept + chokeWidth?
        # goodInterceptPoints: typing.Dict[Tile, InterceptPointTileInfo] = {}
        for tile, interceptInfo in interception.common_intercept_chokes.items():
            if tile.isCity and tile.isNeutral:
                continue
            # # TODO where does this 3 come from...? I think this lets the intercept chase, slightly...?
            # # THE 3 is necessary to chase 1 tile behind. I'm not sure why, though...
            # arbitraryOffset = 3
            # # TODO for final tile in the path, if tile is recapturable (city, normal tile) then increase maxDepth to turnsLeftInCycle
            turnsToIntercept = interceptInfo.max_delay_turns

            depth = min(maxDepth, turnsToIntercept)
            interceptInfo.max_search_dist = depth

        # return
        targetMap = self.map.distance_mapper.get_tile_dist_matrix(interception.target_tile)
        toBypassFullSearch = []
        for tile, interceptInfo in sorted(interception.common_intercept_chokes.items(), key=lambda kvp: targetMap.raw[kvp[0].tile_index]):
            if tile.isCity and tile.isNeutral:
                continue

            depth = interceptInfo.max_search_dist
            currentDist = targetMap.raw[tile.tile_index]
            euclidIntDist = 5
            if currentDist < len(positionsByTurn) and positionsByTurn[currentDist] is not None:
                (x, y) = positionsByTurn[currentDist]
                euclidIntDist = self.map.euclidDist(x, y, tile.x, tile.y)
            reduced = False
            for adj in tile.movable:
                adjIntInfo = interception.common_intercept_chokes.get(adj, None)
                if adjIntInfo is None:
                    continue
                altDist = targetMap.raw[adj.tile_index]
                euclidAdjIntDist = 5
                if altDist < len(positionsByTurn) and positionsByTurn[altDist] is not None:
                    (altX, altY) = positionsByTurn[altDist]
                    euclidAdjIntDist = self.map.euclidDist(altX, altY, adj.x, adj.y)

                # TODO there is no way this is right but it already makes more tests pass than did before...?
                # if adjIntInfo.max_search_dist - euclidAdjIntDist > depth - euclidIntDist:
                offs = 0
                if altDist < currentDist:
                    # dont eliminate later intercepts based on better points closer to the tile that may not be reachable
                    # continue
                    offs = 1

                if euclidAdjIntDist + offs < euclidIntDist:
                    # then we would ALWAYS intercept at the other adjacent, and can skip this one.
                    if self.log_debug:
                        logbook.info(f'\r\n    {str(tile)} Reducing depth to {2 - interceptInfo.max_choke_width} orig depth {depth} + euclidIntDist {euclidIntDist:.2f} due to adjacent {adj} depth {adjIntInfo.max_search_dist} + euclidAdjIntDist {euclidAdjIntDist:.2f}')
                    toBypassFullSearch.append(interceptInfo)
                    reduced = True
                    break
            if self.log_debug and not reduced:
                logbook.info(f'\r\n    {str(tile)} KEPT depth {depth} + euclidIntDist {euclidIntDist:.2f}')

        for intInfo in toBypassFullSearch:
            # intInfo.max_search_dist = min(intInfo.max_search_dist, 1)
            intInfo.max_search_dist = min(intInfo.max_search_dist, 2 - intInfo.max_choke_width)

    def _get_intercept_paths(
            self,
            interceptAtTile: Tile,
            interception: ArmyInterception,
            maxDepth: int,
            turnsLeftInCycle: int,
            threatDistFromCommon: int,
            searchingPlayer: int,
            positionsByTurn: typing.List[typing.Tuple[float, float]],
            otherThreatsBlockingTiles: typing.Dict[Tile, ThreatBlockInfo] | None = None
    ) -> typing.Dict[int, Path]:
        # negs = set()
        # if not self.map.is_tile_friendly(tile):
        #     negs.add(tile)

        startArmy = 0
        if self.map.is_tile_on_team_with(interceptAtTile, searchingPlayer):
            startArmy = 0 - interceptAtTile.army

        threatTile = interception.threats[0].path.start.tile


        def valueFunc(curTile: Tile, prioObj):
            (
                dist,
                euclidIntDist,
                negTileCapPoints,
                negArmy,
                fromTile
            ) = prioObj

            if curTile.player != searchingPlayer:
                return None

            if curTile.army <= 1:
                return None

            if negArmy > 0:
                return None

            recapVal = 0 - (negTileCapPoints + negArmy)

            a = self.map

            return (
                recapVal,
                0 - negTileCapPoints,
                0 - negArmy
            )

        threatDistMap = self.map.distance_mapper.get_tile_dist_matrix(threatTile)
        numPositions = len(positionsByTurn)

        def prioFunc(nextTile: Tile, prioObj):
            (
                dist,
                euclidIntDist,
                negTileCapPoints,
                negArmy,
                toTile
            ) = prioObj

            dist += 1

            if interception.furthest_common_intercept_distances.raw[nextTile.tile_index] > threatDistFromCommon + 1:
                return None

            if nextTile.isCity and nextTile.isNeutral:
                return None

            if self.map.is_tile_on_team_with(nextTile, searchingPlayer):
                negArmy -= nextTile.army
            else:
                negArmy += nextTile.army
                negTileCapPoints += 1
                if not nextTile.isNeutral:
                    negTileCapPoints += 1

            negArmy += 1

            # newDist =
            distTuple = positionsByTurn[dist] if dist < numPositions else None
            if distTuple:
                approxPosX, approxPosY = distTuple
                isApproxMtn = self.map.grid[round(approxPosY)][round(approxPosX)].isObstacle
                # TODO this can exclude the sqrt part of euclid...

                if toTile is not None and not isApproxMtn:
                    euclidIntDist = self.map.euclidDist(approxPosX, approxPosY, nextTile.x, nextTile.y)
                    # TODO needs to switch to negative tiles, and only be supplied when in actual danger and need to intercept with OTHER tiles than the negative tiles, EG last second defense
                    # threatBlock = otherThreatsBlockingTiles.get(nextTile, None)
                    # if threatBlock and threatBlock.amount_needed_to_block > nextTile.army:
                    # if threatBlock and toTile in threatBlock.blocked_destinations:
                    #     return None
                    # if toTile in threatBlock.blocked_destinations:
                    distA = threatDistMap.raw[toTile.tile_index]
                    distB = threatDistMap.raw[nextTile.tile_index]
                    if distA is None:
                        # if not DebugHelper.IS_DEBUGGING:
                        #     return None
                        raise AssertionError(f'{repr(interceptAtTile)}->{repr(toTile)}: {distA}')
                    if distB is None:
                        # if not DebugHelper.IS_DEBUGGING:
                        #     return None
                        raise AssertionError(f'{repr(interceptAtTile)}->{repr(nextTile)}: {distB}')

                    # TODO THIS IS NO LONGER VALID ALONE BECAUSE WE PRUNE POOR ADJACENCIES, SO WE NO LONGER CHECK EVERYTHING CLOSER, THERE ARE CASES WHERE WE PLAN PARALLELS NEXT TO THINGS. SEE test_should_continue_to_intercept_army
                    if distA > distB:
                        # return None
                        toEuclidDist = self.map.euclidDist(approxPosX, approxPosY, toTile.x, toTile.y)
                        # if toEuclidDist > euclidIntDist + 0.8 and toEuclidDist >= 1:  # this fails when trying to intercept threats that could go either way around a mountain.
                        if toEuclidDist > euclidIntDist + 0.8 and toEuclidDist >= 1:
                            # we're moving away from the intercept... we were closer last move.
                            if self.log_debug:
                                logbook.info(f'skipping {nextTile} to {toTile} because toEuclidDist {toEuclidDist:.3f} > euclidIntDist {euclidIntDist:.3f} (approxX {approxPosX:.2f}, approxY {approxPosY:.2f})')
                            return None
                        else:
                            if self.log_debug:
                                logbook.info(f'ALLOWING {nextTile} to {toTile} because toEuclidDist {toEuclidDist:.3f} < euclidIntDist {euclidIntDist:.3f} (approxX {approxPosX:.2f}, approxY {approxPosY:.2f})')

                #
                # prevDistTuple = positionsByTurn.get(dist - 1, None)
                # if prevDistTuple:
                #     prevApproxPosX, prevApproxPosY = prevDistTuple
                #     prevEuclidIntDist = self.map.euclidDistExp(prevApproxPosX, prevApproxPosY, nextTile.x, nextTile.y)
                #     if prevEuclidIntDist < euclidIntDist:  # prevEuclidIntDist + 0.33
                #         # we're moving away from the intercept... we were closer last move.
                #         return None
            else:
                # happens when we intercept further than the threat length eg defending a city (?)
                pass
                #euclidIntDist = 100
                # raise Exception(f'This shouldnt be possible {nextTile}  <-  {toTile}  dist {dist}')

            return (
                dist,
                euclidIntDist,
                negTileCapPoints,
                negArmy,
                nextTile
            )

        # TODO can we just combine searches into one big search...? this is already per tile per distance...
        startTiles = {interceptAtTile: ((0, 0, 0, startArmy, interceptAtTile), 0)}
        results = SearchUtils.breadth_first_dynamic_max_per_tile_per_distance(
            self.map,
            startTiles=startTiles,
            valueFunc=valueFunc,
            maxDepth=maxDepth,
            noNeutralCities=True,
            priorityFunc=prioFunc,
            logResultValues=self.log_debug
        )

        paths = results.get(interceptAtTile, [])

        byDist = {}
        if self.log_debug:
            logbook.info(f'@{str(interceptAtTile)} depth{maxDepth} returned {len(paths)} paths.')
        for path in paths:
            revPath = path.get_reversed()
            if self.log_debug:
                logbook.info(f'  path len {revPath.length} -- {str(revPath)}')
            byDist[revPath.length] = revPath

        return byDist

    def _get_value_of_threat_blocked(self, interception: ArmyInterception, interceptPath: Path, best_enemy_threat_info: ThreatValueInfo, turnsLeftInCycle: int) -> typing.Tuple[Tile, float, int, int, int, int]:
        """
        Returns enInterceptPointTile, econValueBlocked, enemyArmyRemainingAtIntercept, enemyArmyIntercepted, bestCaseInterceptTurns, worstCaseInterceptTurns (if any).

        enInterceptPointTile: the tile at which the intercept happens (not necessarily the last tile the enemy army captures, if not full blocked).
        econValueBlocked: the econ value of the opponents attack that is blocked before round end. If enemy can still complete captures to round end despite the block, this will be 0.
        enemyArmyRemainingAtIntercept: The amount of enemy army left at intercept. Negative if we have recapture army left over.
        enemyArmyIntercepted: The amount of enemy army that gets collided with at the intercept point.
        bestCaseInterceptTurns: the earliest the intercept could happen if they walk into us.
        worstCaseInterceptTurns: the latest the intercept could happen if they go on the expected intercept courses but choose the most adversarial route.
        """
        best_enemy_threat = best_enemy_threat_info.threat
        blockable = best_enemy_threat_info.econ_value

        # TODO we dont need to recalculate this for the enemy threat every time...
        (
            turnsLeft,
            armyAccumulatedByInterceptPath,
            bestCaseInterceptTurn,
            worstCaseInterceptTurn,
            enInterceptPointPathNode,
            enPhysicalArmyAtTile,
        ) = self._get_result_of_executing_paths_to_intercept_point(interception, best_enemy_threat, interceptPath, turnsLeftInCycle)
        if self.log_debug:
            logbook.info(f'_get_result_of_executing_paths_to_intercept_point {interceptPath}:' +
                        f'\nturnsLeft: {turnsLeft}' +
                        f'\narmyAccumulatedByInterceptPath: {armyAccumulatedByInterceptPath}' +
                        f'\nbestCaseInterceptTurn: {bestCaseInterceptTurn}' +
                        f'\nworstCaseInterceptTurn: {worstCaseInterceptTurn}' +
                        f'\nenInterceptPointPathNode: {enInterceptPointPathNode}' +
                        f'\nenPhysicalArmyAtTile: {enPhysicalArmyAtTile}'
            )

        enInterceptPointTile = enInterceptPointPathNode.tile

        enArmy = int(best_enemy_threat.path.value)
        """The army moving backwards from the tail tile... This probably doesnt work right..."""
        interceptDistFromTarget = best_enemy_threat.armyAnalysis.bMap[interceptPath.tail.tile]
        if best_enemy_threat.path.tail.tile.isGeneral and not self.map.player_has_priority_over_other(best_enemy_threat.path.tail.tile.player, best_enemy_threat.threatPlayer, self.map.turn + best_enemy_threat.path.length - 1):
            # this feels like a hack... we shouldnt need this......
            interceptDistFromTarget += 1

        # This assumes the intercept is moving towards the threat, pretty sure
        enLen = best_enemy_threat.path.length

        turnsLeft = turnsLeftInCycle
        # skip backwards until the threat is capturing tiles (eg for threats that send a 20 army through our 40 to our general, or whatever)
        node = best_enemy_threat.path.tail
        while node is not None and enArmy < 0 and enLen > interceptDistFromTarget:
            if self.map.is_tile_on_team_with(node.tile, best_enemy_threat.threatPlayer):
                enArmy -= node.tile.army
            else:
                enArmy += node.tile.army

            # we're going backwards, so we add 1...?
            enArmy += 1
            node = node.prev
            enLen -= 1

        if node != best_enemy_threat.path.tail:
            if self.log_debug:
                logbook.info(f'backed enemy threat from {best_enemy_threat.path.tail.tile} to {node.tile} because it wasn\'t capturing past that point')

        enArmyAtFinalCaptureBeforeBlockCalculation = enArmy
        enArmy -= armyAccumulatedByInterceptPath
        # everything before this point that we already .prev'd was stuff they couldn't have econ'd anyway.
        econValueBlocked = 0
        mainEconDamageComplete = False

        # while node is not None and enArmy < 0 and enLen > interceptDistFromTarget and turnsLeft > 0:
        # work backwards to find all the tiles they DONT capture
        while node is not None:
            # if node.tile == enInterceptPointTile:
            #     break
            if turnsLeft <= 0:  # TODO this is wrong, we are giving them credit for the last captures in their path instead of the first captures in their path... Instead we need to back their path off similar to the 'negative army' loop above?
                mainEconDamageComplete = True

            if enArmy >= 0:
                break
            if enLen <= interceptDistFromTarget:
                break

            if self.map.is_tile_on_team_with(node.tile, best_enemy_threat.threatPlayer):
                enArmy -= node.tile.army
            else:
                enArmy += node.tile.army
                if self.map.is_tile_friendly(node.tile):
                    if node.tile.isGeneral:
                        econValueBlocked += GENERAL_CAP_VALUE
                    elif node.tile.isCity:
                        if not mainEconDamageComplete:
                            cappedTurnsToRoundEnd = turnsLeftInCycle - enLen
                            # reward the amount of the number of turns they'd hold the city till round end. Because they're capping OUR city, we lose 0.5 econ per turn and they gain 0.5 econ per turn so its 1 per turn. Really 2 per cityBonusTurn but idk if we care to get that granular.
                            econValueBlocked += cappedTurnsToRoundEnd
                            # base offset reward for preventing city capture
                        econValueBlocked += TARGET_CITY_FLAT_BONUS
                    if not mainEconDamageComplete:
                        econValueBlocked += TARGET_CAP_VALUE
                elif node.tile.player >= 0:
                    if not mainEconDamageComplete:
                        econValueBlocked += NEUTRAL_CAP_VALUE
                else:
                    # give worse reward for blocking attacks into other enemy territory, I guess?
                    if not mainEconDamageComplete:
                        econValueBlocked += OTHER_PARTY_CAP_VALUE
            if self.log_debug:
                logbook.info(f'    node {node.tile}  turnsLeft {turnsLeft}  enArmy {enArmy}  econValueBlocked {econValueBlocked}  enLen {enLen}')

            enArmy += 1
            node = node.prev
            enLen -= 1
            turnsLeft -= 1

        # leftoverEnArmy = enArmy
        # leftoverEnLen = enLen
        # leftoverNode = node
        #
        # while leftoverNode is not None:
        #     leftoverEnArmy += 1
        #     leftoverNode = leftoverNode.prev
        #     leftoverEnLen -= 1

        enemyArmyCollidedWithAtIntercept = enArmy + armyAccumulatedByInterceptPath
        enemyArmyLeftAtInterceptPointBeforeRemainingCapture = enArmy
        if node.tile == enInterceptPointTile:
            # determine intercept remainder. This means we stopped the army AT the intercept, effectively means this is a capture, no enemy army remaining.
            if self.log_debug:
                logbook.info(f'broke at tile being the one the army was at when our intercept path ended...? {node.tile}')
            # TODO figure out if we need to take one more step / delay and chase and factor that in to the values and turns.

            #
            # while turnsUsed < worstCaseInterceptTurn:
            #     logbook.info(f'assuming worst case intercept moves')
            #     turnsUsed += 1


        else:
            # then either we didn't fully block at the intercept, or something else weird happened.
            if enemyArmyLeftAtInterceptPointBeforeRemainingCapture <= 0:
                if self.log_debug:
                    logbook.error(f'wtf broke somewhere other than the expected intercept spot? node.tile {node.tile} vs enInterceptPointTile {enInterceptPointTile}. enemyArmyLeftAtInterceptPointBeforeRemainingCapture {enemyArmyLeftAtInterceptPointBeforeRemainingCapture}')

        if self.log_debug:
            logbook.info(f'blocked {econValueBlocked} econ dmg, ({armyAccumulatedByInterceptPath} army), at interceptDist {interceptDistFromTarget}, enemy army left at intercept {enemyArmyLeftAtInterceptPointBeforeRemainingCapture}. bestCaseInterceptTurn {bestCaseInterceptTurn}, worstCaseInterceptTurn {worstCaseInterceptTurn}: path {str(interceptPath)}')

        return enInterceptPointTile, econValueBlocked, enemyArmyLeftAtInterceptPointBeforeRemainingCapture, enemyArmyCollidedWithAtIntercept, bestCaseInterceptTurn, worstCaseInterceptTurn

    def _get_result_of_executing_paths_to_intercept_point(self, interception: ArmyInterception, best_enemy_threat: ThreatObj, interceptPath: Path, turnsLeftInCycle: int):
        """
        returns turnsLeft, armyAccumulatedByInterceptPath, bestCaseInterceptTurn, worstCaseInterceptTurn, enInterceptPointTile, enPhysicalArmyAtTile

        @param best_enemy_threat:
        @param interceptPath:
        @param turnsLeftInCycle:
        @return:
        """
        enPathNode = best_enemy_threat.path.start
        enPhysicalArmyByTile = []
        threatPlayers = self.map.get_teammates(best_enemy_threat.threatPlayer)
        enPhysicalArmy = 0
        # TODO METHOD
        armyAccumulatedByInterceptPath = 0
        bestCaseInterceptTurn = 1000
        worstCaseInterceptTurn = 1000
        enInterceptPointPathNode = None
        turnsLeft = turnsLeftInCycle  # - 1??? We're tracking the turn the move executes, not the turn the move is played, I suppose thats fine.
        turnsUsed = 0
        hasPrio = self.map.player_has_priority_over_other(self.map.player_index, best_enemy_threat.threatPlayer, self.map.turn)
        for i, tile in enumerate(interceptPath.tileList):
            hasPrio = not hasPrio
            if turnsLeft == 0:
                break
            if enPathNode is None:
                break

            if enPathNode.tile.player in threatPlayers:
                enPhysicalArmy += enPathNode.tile.army - 1
            else:
                enPhysicalArmy -= enPathNode.tile.army + 1
            enInterceptPointPathNode = enPathNode
            enPhysicalArmyByTile.append(enPhysicalArmy)

            turnsLeft -= 1
            if tile not in best_enemy_threat.path.tileSet:
                if self.map.is_tile_friendly(tile):
                    armyAccumulatedByInterceptPath += tile.army - 1
                else:
                    armyAccumulatedByInterceptPath -= tile.army + 1

            tilesAtDist = best_enemy_threat.armyAnalysis.tileDistancesLookup.get(i, None)
            if tilesAtDist and i > 0:
                allInMovable = len(tilesAtDist) < 4
                directIntercept = False
                for t in tilesAtDist:
                    if tile in t.movable:
                        # + 2 because of chase moves...?
                        offs = 1
                        if not hasPrio:
                            offs += 1
                        bestCaseInterceptTurn = min(bestCaseInterceptTurn, i + offs)
                    elif t == tile:
                        if len(tilesAtDist) == 1:
                            directIntercept = True
                        bestCaseInterceptTurn = min(bestCaseInterceptTurn, i)
                    elif allInMovable:  # short circuit the adjacents check early if we've already failed the all-in-movable test.
                        allInMovable = False

                if directIntercept:
                    worstCaseInterceptTurn = min(i, worstCaseInterceptTurn)
                elif allInMovable:
                    offs = 1
                    if not hasPrio:
                        offs += 1
                    worstCaseInterceptTurn = min(i + offs, worstCaseInterceptTurn)
                else:
                    # intDist = best_enemy_threat.armyAnalysis.interceptDistances.raw[tile.tile_index]
                    # if intDist is not None:
                    #     worstCaseInterceptTurn = min(intDist + i, worstCaseInterceptTurn)
                    intInf = interception.common_intercept_chokes.get(tile, None)
                    if intInf is not None:
                        # this is correct, (at least in current unit test) however we just need to incorporate the fact that we may need to delay a turn, and then chase a turn (?)
                        worstCaseInterceptTurn = min(intInf.max_extra_moves_to_capture + i, worstCaseInterceptTurn)

            enPathNode = enPathNode.next

        turnsUsed = turnsLeftInCycle - turnsLeft
        while turnsUsed < worstCaseInterceptTurn and enPathNode:
            turnsUsed += 1
            turnsLeft -= 1

            if enPathNode.tile.player in threatPlayers:
                enPhysicalArmy += enPathNode.tile.army - 1
            else:
                enPhysicalArmy -= enPathNode.tile.army + 1
            enInterceptPointPathNode = enPathNode
            enPhysicalArmyByTile.append(enPhysicalArmy)

            enPathNode = enPathNode.next

        if enPathNode is None:
            if self.log_debug:
                logbook.error(f'we got to the end of the enemy path before we got past our worst case intercept turn {worstCaseInterceptTurn} (turnsUsed {turnsUsed})')
        #
        # if interceptPath.tail.tile not in best_enemy_threat.path.tileSet:
        #     found = None
        #     for adj in interceptPath.tail.tile.movableNoObstacles:
        #         if adj in best_enemy_threat.path.tileSet:
        #             found = adj
        #             break
        #
        #     if not found:
        #         logbook.error(f'wtf failed to find a move connecting int tail {interceptPath.tail.tile} to threat path? enInterceptPointTile {enInterceptPointPathNode.tile}')
        #     else:
        #         # turnsLeft -= 1  # we have to make one extra move to reach the threat, since we found threat path in movable. This might already be covered in our 'worst case intercept' number, though...?
        #         intFwd = enInterceptPointPathNode
        #         intBack = enInterceptPointPathNode
        #         numFwd = 0
        #         numBack = 0
        #         while found != intFwd.tile and found != intBack.tile:
        #             ended = True
        #             if intFwd.next is not None:
        #                 intFwd = intFwd.next
        #                 numFwd += 1
        #                 ended = False
        #             if intBack.prev is not None:
        #                 intBack = intBack.prev
        #                 numBack += 1
        #                 ended = False
        #
        #             if ended:
        #                 break
        #
        #         if intFwd.tile == found:
        #             if self.log_debug:
        #                 logbook.info(f'found actual intercept point forward {numFwd} at {intFwd.tile}')
        #             enInterceptPointPathNode = intFwd
        #         elif intBack.tile == found:
        #             if self.log_debug:
        #                 logbook.info(f'found actual intercept point backward {numBack} at {intBack.tile}')
        #             if numBack > 1:
        #                 raise AssertionError(f'we literally can\'t intercept backwards by more than 1 tile, but met {numBack} backwards....')
        #             enInterceptPointPathNode = intBack
        #         else:
        #             logbook.error(f'found no connection to the threat path wtf for {interceptPath.tail.tile} -> threat {best_enemy_threat.path}')

        movesToThisPoint = turnsLeftInCycle - turnsLeft

        logbook.info(f'We expect to intercept int{interceptPath.tail.tile} {armyAccumulatedByInterceptPath}a -> enIntP{enInterceptPointPathNode.tile} {enPhysicalArmy}a after {movesToThisPoint} turns with {turnsLeft} left in cycle.'
                     f'\r\n     bestCaseInterceptTurn {bestCaseInterceptTurn}, worstCaseInterceptTurn {worstCaseInterceptTurn}')

        return turnsLeft, armyAccumulatedByInterceptPath, bestCaseInterceptTurn, worstCaseInterceptTurn, enInterceptPointPathNode, enPhysicalArmy

    def get_intercept_blocking_tiles_for_split_hinting(
            self,
            tile: Tile,
            threatsByTile: typing.Dict[Tile, typing.List[ThreatObj]],
            negativeTiles: typing.Set[Tile] | None = None,
    ) -> typing.Dict[Tile, ThreatBlockInfo]:
        """
        Returns a map from tile to the amount of army that should be left on them in order to block multiple threats.

        @param tile:
        @param threatsByTile:
        @param negativeTiles: tiles to just straight up block their value on, in addition to actual threats.
        @return:
        """
        blockingTiles = {}

        if negativeTiles:
            for chokeTile in negativeTiles:
                blockInfo = blockingTiles.get(chokeTile, None)
                if blockInfo is None:
                    blockInfo = ThreatBlockInfo(chokeTile, chokeTile.army)
                    blockingTiles[chokeTile] = blockInfo
                    for mv in chokeTile.movable:
                        blockInfo.add_blocked_destination(mv)

        for otherTile, otherThreats in threatsByTile.items():
            # blockOnMultiple = False
            # if otherTile == tile:
            #     blockOnMultiple = True
            #     # continue

            # for chokeTile in plan.common_intercept_choke_widths.keys():
            #     if not self._map.is_tile_friendly(chokeTile):
            #         continue
            #     if chokeTile.army < tile.army // 3:
            #         continue
            #
            #     blockingTiles.add(chokeTile)
            for threat in otherThreats:
                realThreatVal = threat.path.calculate_value(threat.threatPlayer, self.map.team_ids_by_player_index, doNotSaveToPath=True)
                self.ensure_threat_army_analysis(threat)
                if threat.armyAnalysis.shortestPathWay is None or not threat.armyAnalysis.shortestPathWay.tiles:
                    logbook.error(f'Yo, wtf? Shortest pathway for threat {threat} had no tiles...?')
                    continue
                gen = self.map.generals[self.map.player_index]
                towardsUs = self.map.get_distance_between(gen, threat.path.start.tile) - self.map.get_distance_between(gen, threat.path.tail.tile) > 0
                if not towardsUs:
                    continue

                for chokeTile in threat.path.tileList:
                    if not self.map.is_tile_friendly(chokeTile):
                        continue
                    if chokeTile.army < otherTile.army // 3:
                        continue

                    # blockAmount = threat.threatValue
                    blockAmount = realThreatVal + chokeTile.army
                    blockInfo = blockingTiles.get(chokeTile, None)
                    if blockInfo is None:
                        blockInfo = ThreatBlockInfo(chokeTile, blockAmount)
                        blockingTiles[chokeTile] = blockInfo

                    canDie = realThreatVal > 0 and threat.threatType == ThreatType.Kill

                    for moveable in chokeTile.movable:
                        # if moveable not in threat.armyAnalysis.shortestPathWay.tiles:
                        if canDie or (moveable not in threat.path.tileSet and (moveable not in threat.armyAnalysis.shortestPathWay.tiles or threat.armyAnalysis.bMap.raw[moveable.tile_index] >= threat.armyAnalysis.bMap.raw[chokeTile.tile_index])):
                            blockInfo.add_blocked_destination(moveable)

                    if blockInfo.amount_needed_to_block < blockAmount:
                        blockInfo.amount_needed_to_block = blockAmount
                        # blockingTiles[chokeTile] = blockInfo

        return blockingTiles

    def _should_delay_or_split(
            self,
            interceptEndTile: Tile,
            interceptionPath: Path,
            threats: typing.List[ThreatValueInfo],
            turnsLeftInCycle: int
    ) -> typing.Tuple[bool, bool]:
        """
        Returns shouldDelay, shouldSplit

        TODO incomplete, add more logic here to decide when splitting is ideal

        @param interceptEndTile:
        @param interceptionPath:
        @param threats:
        @param turnsLeftInCycle:
        @return:
        """
        shouldSplit = False
        shouldDelay = False
        firstThreat = threats[0].threat
        isTwoAway = firstThreat.armyAnalysis.bMap[interceptionPath.start.tile] == 2

        if not isTwoAway:
            return shouldDelay, shouldSplit

        allowSplit = True

        threatNexts = set()
        usNexts = set()
        for threatInfo in threats:
            tilesAtDist = threatInfo.threat.armyAnalysis.tileDistancesLookup.get(1, None)

            threatBase = threatInfo.threat.path.start.tile.army

            if tilesAtDist:
                for threatNext in tilesAtDist:
                    if self.map.is_tile_on_team_with(threatNext, threatInfo.threat.threatPlayer):
                        threatArmy = threatBase + threatNext.army - 1
                    else:
                        threatArmy = threatBase - threatNext.army - 1

                    halfLower = interceptionPath.start.tile.army // 2
                    # if halfLower - 2 < threatArmy:  # TODO dunno why this was -2, but making this over-split to find counter-examples where we dont want it to, I guess.
                    if halfLower < threatArmy - 1:
                        allowSplit = False

                    threatNexts.add(threatNext)
                    if threatNext in interceptionPath.start.tile.movable:
                        usNexts.add(threatNext)

        if len(threatNexts) > 1 and len(usNexts) > 1 and threatNexts.issubset(usNexts):
            if not allowSplit:
                shouldDelay = True
            else:
                shouldSplit = True

        # cant do both, and delay wins. After we delay, we can decide whether to split again next turn.
        if shouldDelay:
            shouldSplit = False

        return shouldDelay, shouldSplit



