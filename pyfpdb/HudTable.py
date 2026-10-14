#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#    Copyright 2026
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
"""The poker table the replayer draws: seats around an oval, real card art, bets and tags.

This module knows nothing about databases or hand histories. HudReplayer works out what one moment
of a hand looks like and hands it to PokerTable.set_state as plain ints, lists and dicts.

Everything about *where* things go lives in `layout`, which returns rectangles and paints nothing.
Painting consumes those rectangles, and a test can walk every frame of every imported hand and
assert that no two boxes overlap -- that is how the numbers in `sizes_for` were picked, because one
screenshot cannot cover 6-max, Omaha fans, all-ins and every window size at once. Measured against
that walk (every frame of the 345 imported hands, at five felts from the 688x358 one this widget's
own minimum size gives up to 1393x760, and in both money units since big-blind strings are wider
than the dollars they replace): no overlapping pair. A bet pill that the board blocks from sitting in
front of its own cards tucks round to the cards' inner side instead.
"""
import math
import os

from PySide6 import QtCore, QtGui, QtWidgets, QtSvg

import Configuration
import Card

# gfx/cards/white holds the 52 cards as `<suit>_<rank>.svg` on a 140x190 viewBox, plus two backs.
CARD_DIR = os.path.join(Configuration.GRAPHICS_PATH, "cards")
FACE_DIR = os.path.join(CARD_DIR, "white")
BACK_SVG = os.path.join(CARD_DIR, "backs", "back03.svg")
CARD_RATIO = 140.0 / 190.0
RANK_FILE = {"A": "a", "K": "k", "Q": "q", "J": "j", "T": "10"}

_cache = {}

FELT_TOP = QtGui.QColor("#1f6349")
FELT_BOTTOM = QtGui.QColor("#0b2b1f")
RAIL = QtGui.QColor("#2a1a0e")
PLATE = QtGui.QColor("#1b2530")
PLATE_FOLDED = QtGui.QColor("#242a31")
HERO_EDGE = QtGui.QColor("#3d88e0")
WINNER_EDGE = QtGui.QColor("#39b36b")
ACTOR_EDGE = QtGui.QColor("#e8c15a")
MUTED = QtGui.QColor("#9fb3c8")
GOLD = QtGui.QColor("#e8c15a")
RED = QtGui.QColor("#ff6b5e")


def card_pixmap(card_id, height):
    """One card face, rendered from the repo's SVG at the given pixel height and cached."""
    key = ("card", card_id, height)
    if key not in _cache:
        name = Card.valueSuitFromCard(card_id)
        rank = RANK_FILE.get(name[:-1], name[:-1].lower())
        path = os.path.join(FACE_DIR, "%s_%s.svg" % (name[-1].lower(), rank))
        _cache[key] = _render(path, height)
    return _cache[key]


def card_back(height):
    key = ("back", height)
    if key not in _cache:
        _cache[key] = _render(BACK_SVG, height)
    return _cache[key]


def _render(path, height):
    renderer = QtSvg.QSvgRenderer(path)
    width = int(round(height * CARD_RATIO))
    pixmap = QtGui.QPixmap(width, height)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    if renderer.isValid():
        renderer.render(painter, QtCore.QRectF(0, 0, width, height))
    else:
        painter.setBrush(QtGui.QColor("#f7f7f7"))
        painter.setPen(QtGui.QPen(QtGui.QColor("#999999")))
        painter.drawRoundedRect(pixmap.rect(), 4, 4)
    painter.end()
    return pixmap


def font(pixel_size, bold=False):
    text = QtGui.QFont("Segoe UI")
    text.setPixelSize(max(7, int(pixel_size)))
    text.setBold(bold)
    return text


def text_width(text, qfont):
    return QtGui.QFontMetricsF(qfont).horizontalAdvance(text)


def seat_slots(count):
    """Table positions, as fractions of the felt rect: index 0 bottom centre, then up the right
    side, across the top and down the left.

    That is the direction play passes around a table as the hero sees it, so index 1 is the player
    who acts after him -- on the hero's right, where the live-table button travels. The caller owns
    which player lands in which index; this knows nothing about hands.
    """
    total = max(count, 2)
    slots = []
    for index in range(count):
        theta = math.radians(90.0 - index * 360.0 / total)
        slots.append((0.5 + 0.405 * math.cos(theta), 0.5 + 0.425 * math.sin(theta)))
    return slots


def support(width, height, dx, dy):
    """Half of how far an axis-aligned box reaches along a unit direction.

    A seat's cards have to clear its own name plate, and a plate is 150 px wide but only 40 tall:
    along the radius at the bottom of the table the clearance is the plate's half height, while at
    the sides it is the half width. This is the one number that makes both come out right.
    """
    return abs(width * dx * 0.5) + abs(height * dy * 0.5)


def clamp(rect, felt):
    """Slide a box back inside the felt so no seat is cut off on a small window."""
    moved = QtCore.QRectF(rect)
    if moved.left() < felt.left() + 4:
        moved.moveLeft(felt.left() + 4)
    if moved.right() > felt.right() - 4:
        moved.moveRight(felt.right() - 4)
    if moved.top() < felt.top() + 4:
        moved.moveTop(felt.top() + 4)
    if moved.bottom() > felt.bottom() - 4:
        moved.moveBottom(felt.bottom() - 4)
    return moved


def bounded(value, low, high):
    return max(low, min(value, high))


def clear_of(width_a, height_a, width_b, height_b, dx, dy, gap):
    """How far along a unit direction box b must move from box a's centre to stop overlapping it.

    Not the support function: two axis-aligned boxes on a shallow ray still intersect long after
    their corners part along the ray, so the real answer is the first axis to separate.
    """
    reach = []
    for component, extent in ((dx, (width_a + width_b) / 2.0 + gap),
                              (dy, (height_a + height_b) / 2.0 + gap)):
        if abs(component) > 1e-9:
            reach.append(extent / abs(component))
    return min(reach) if reach else max(width_a, width_b) / 2.0 + gap


def ray_hit(point, unit, rect):
    """How far along a ray the first entry into an axis-aligned rect is, or None if never.

    Used to stop a seat's chips from being pushed into the board, so the caller can pull the box
    back along the same line it came.
    """
    tmin, tmax = 0.0, float("inf")
    for start, direction, low, high in ((point.x(), unit.x(), rect.left(), rect.right()),
                                        (point.y(), unit.y(), rect.top(), rect.bottom())):
        if abs(direction) < 1e-9:
            if not low <= start <= high:
                return None
            continue
        first, second = (low - start) / direction, (high - start) / direction
        if first > second:
            first, second = second, first
        tmin, tmax = max(tmin, first), min(tmax, second)
    return tmin if tmax >= tmin else None


def sizes_for(felt):
    """Box and text sizes for one felt.

    The caps are not taste: they are the largest values at which the layout test finds no
    overlapping pair in any frame of the 345 imported hands, at every window size the widget
    accepts. Width matters as much as height, because a wide short window has no room for the
    board row and the two side seats' cards to both fit across it.
    """
    cards_h = bounded(min(felt.height() * 0.115, felt.width() * 0.052), 16.0, 44.0)
    board_h = bounded(min(felt.height() * 0.150, felt.width() * 0.050), 18.0, 62.0)
    plate_h = bounded(felt.height() * 0.075, 26.0, 42.0)
    bet_h = bounded(felt.height() * 0.055, 12.0, 20.0)
    return {"plate_w": bounded(felt.width() * 0.145, 78.0, 165.0), "plate_h": plate_h,
            "cards_h": cards_h, "card_w": cards_h * CARD_RATIO, "board_h": board_h,
            "bet_h": bet_h, "spacing": bounded(felt.height() * 0.030, 7.0, 14.0),
            "name_px": bounded(plate_h * 0.30, 8.0, 13.0),
            "sub_px": bounded(plate_h * 0.26, 7.0, 12.0),
            "bet_px": bounded(bet_h * 0.62, 8.0, 13.0),
            "pot_px": bounded(board_h * 0.30, 8.0, 14.0),
            "badge_d": bounded(plate_h * 0.42, 11.0, 17.0)}


def card_cluster(count, card_w):
    """Width and per-card step of a fan. Omaha's four cards overlap so a seat on the side of the
    table cannot push its cards out as far as a plate-and-a-half."""
    step = card_w + 3.0 if count < 3 else card_w * 0.55
    return step * (max(1, count) - 1) + card_w, step


def chip_rect(pill):
    """The gold chip at the left end of a money pill, sized to that pill's height."""
    d = pill.height() * 0.42
    return QtCore.QRectF(pill.left() + 5, pill.center().y() - d / 2.0, d, d)


def board_boxes(felt, state, sizes):
    """The five card slots in the middle, and the pot pill above them.

    The pill carries only the pot: everything else the replayer knows (which board run this is,
    what the last action was) belongs on the controls, where it cannot crowd the felt.
    """
    height = sizes["board_h"]
    one_w = height * CARD_RATIO
    gap = height * 0.08
    total_w = 5 * one_w + 4 * gap
    top = felt.center().y() - height / 2.0 - 8
    left = felt.center().x() - total_w / 2.0
    boxes = {"card": [QtCore.QRectF(left + index * (one_w + gap), top, one_w, height)
                      for index in range(5)]}
    pot_h = sizes["pot_px"] * 1.8
    pot_w = text_width(state["pot_text"], font(sizes["pot_px"], True)) + pot_h * 1.4
    boxes["pot"] = _centred(QtCore.QRectF(0, 0, pot_w, pot_h),
                            QtCore.QPointF(felt.center().x(), top - pot_h * 0.5 - 6))
    return boxes


def _separated(a, b, gap=1.5):
    """True when two boxes do not really sit on each other: overlap needs both dimensions."""
    hit = a.intersected(b)
    return hit.width() <= gap or hit.height() <= gap


def _fits(rect, felt, blockers):
    """The rect slid into the felt, or None if that leaves it on one of the blockers."""
    moved = clamp(rect, felt)
    for blocker in blockers:
        if not _separated(moved, blocker):
            return None
    return moved


def seat_boxes(felt, table_centre, centre, seat, sizes, keepout=None):
    """One seat as a stack from the rail inwards: plate, cards, then the bet.

    ALL-IN and the winning amount are text inside the plate rather than boxes outside it: a pill on
    the plate's inner edge sits exactly where that seat's own cards land.
    """
    plate_w, plate_h = sizes["plate_w"], sizes["plate_h"]
    plate = clamp(_centred(QtCore.QRectF(0, 0, plate_w, plate_h), centre), felt)
    toward = table_centre - centre
    length = math.hypot(toward.x(), toward.y())
    if length < 1e-6:
        toward, length = QtCore.QPointF(0, -1), 1.0
    unit = QtCore.QPointF(toward.x() / length, toward.y() / length)
    dx, dy = unit.x(), unit.y()

    def how_far(rect, origin):
        """How far the rect's centre may travel inward from origin before it reaches the board."""
        if keepout is None:
            return float("inf")
        hit = ray_hit(origin, unit, keepout.adjusted(-rect.width() / 2.0 - 2,
                                                     -rect.height() / 2.0 - 2,
                                                     rect.width() / 2.0 + 2,
                                                     rect.height() / 2.0 + 2))
        return float("inf") if hit is None else max(0.0, hit)

    def place(rect, origin, desired):
        """Push a box out along the seat's radius, stopping short of the board.

        A box's centre may come within the keep-out as soon as it is half a box away from it, so
        the rect the centre is tested against is the keep-out grown by that much.
        """
        return clamp(_centred(rect, origin + unit * min(desired, how_far(rect, origin))), felt)

    cluster_w, step = card_cluster(max(1, len(seat["cards"])), sizes["card_w"])
    cards_h = sizes["cards_h"]
    cards = place(QtCore.QRectF(0, 0, cluster_w, cards_h), plate.center(),
                  clear_of(plate_w, plate_h, cluster_w, cards_h, dx, dy, sizes["spacing"]))

    boxes = {"plate": plate, "cards": cards, "step": step, "dir": unit, "bet": None,
             "badge": None}
    if seat["bet_text"]:
        width = text_width(seat["bet_text"], font(sizes["bet_px"], True)) + sizes["bet_h"] * 1.3
        bet = QtCore.QRectF(0, 0, width, sizes["bet_h"])
        need = clear_of(cluster_w, cards_h, width, sizes["bet_h"], dx, dy,
                        sizes["spacing"] * 0.3)
        blockers = [cards] + ([keepout] if keepout is not None else [])
        # Straight in is the wish; a seat at the side of a short table has a nearly flat radius,
        # so the board stops the pill before it has cleared its own cards. It then slides along
        # that cards' inner side toward the middle, which is where a real table's chip rail is.
        inboard = cards.center() + unit * min(need, how_far(bet, cards.center()))
        to_centre = table_centre - cards.center()
        spots = sorted((QtCore.QPointF(-dy, dx), QtCore.QPointF(dy, -dx)),
                       key=lambda p: -(p.x() * to_centre.x() + p.y() * to_centre.y()))
        boxes["bet"] = _fits(_centred(bet, inboard), felt, blockers)
        for perp in spots:
            if boxes["bet"] is not None:
                break
            travel = clear_of(cluster_w, cards_h, width, sizes["bet_h"], perp.x(), perp.y(),
                              sizes["spacing"] * 0.3)
            boxes["bet"] = _fits(_centred(bet, inboard + perp * travel), felt, blockers)
    if seat["dealer"]:
        d = sizes["badge_d"]
        boxes["badge"] = _centred(QtCore.QRectF(0, 0, d, d),
                                  plate.topRight() + QtCore.QPointF(-d * 0.5 - 3, d * 0.5 + 3))
    return boxes


def _centred(rect, point):
    moved = QtCore.QRectF(rect)
    moved.moveCenter(point)
    return moved


def layout(felt, state):
    """Rectangles for one frame: the board, the pot, and one box set per seat."""
    sizes = sizes_for(felt)
    boxes = board_boxes(felt, state, sizes)
    keepout = QtCore.QRectF(boxes["pot"])
    for rect in boxes["card"]:
        keepout = keepout.united(rect)
    boxes["seats"] = [
        seat_boxes(felt, QtCore.QPointF(felt.center()),
                   QtCore.QPointF(felt.left() + fx * felt.width(),
                                  felt.top() + fy * felt.height()), seat, sizes, keepout)
        for seat, (fx, fy) in zip(state["seats"], seat_slots(len(state["seats"])))]
    return boxes


class PokerTable(QtWidgets.QWidget):
    """One frame of one hand, drawn as a table."""

    def __init__(self, parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        self.state = None
        self.setMinimumSize(760, 430)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

    def set_state(self, state):
        self.state = state
        self.update()

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        outer = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.fillRect(outer, QtGui.QColor("#20262c"))
        if self.state is None:
            painter.setPen(MUTED)
            painter.setFont(font(outer.height() * 0.06))
            painter.drawText(outer, QtCore.Qt.AlignCenter, "no hand selected")
            painter.end()
            return
        felt = outer.adjusted(26, 26, -26, -26)
        painter.setPen(QtGui.QPen(RAIL, 16))
        painter.setBrush(FELT_BOTTOM)
        painter.drawRoundedRect(felt, felt.height() / 2.0, felt.height() / 2.0)
        gradient = QtGui.QLinearGradient(0, felt.top(), 0, felt.bottom())
        gradient.setColorAt(0.0, FELT_TOP)
        gradient.setColorAt(1.0, FELT_BOTTOM)
        inner = felt.adjusted(9, 9, -9, -9)
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 28), 1))
        painter.setBrush(gradient)
        painter.drawRoundedRect(inner, inner.height() / 2.2, inner.height() / 2.2)

        boxes = layout(inner, self.state)
        sizes = sizes_for(inner)
        self._paint_board(painter, self.state, boxes, sizes)
        for seat, seat_boxes_ in zip(self.state["seats"], boxes["seats"]):
            self._paint_seat(painter, seat, seat_boxes_, sizes)
        painter.end()

    def _paint_board(self, painter, state, boxes, sizes):
        for index, rect in enumerate(boxes["card"]):
            if index < len(state["board"]):
                painter.drawPixmap(rect.topLeft(),
                                   card_pixmap(state["board"][index], int(rect.height())))
            else:
                painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 45), 1, QtCore.Qt.DashLine))
                painter.setBrush(QtGui.QColor(255, 255, 255, 10))
                painter.drawRoundedRect(rect, 4, 4)
        pot = boxes["pot"]
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor(6, 12, 16, 210))
        painter.drawRoundedRect(pot, pot.height() / 2.0, pot.height() / 2.0)
        chip = chip_rect(pot)
        painter.setBrush(GOLD)
        painter.drawEllipse(chip)
        painter.setFont(font(sizes["pot_px"], True))
        painter.setPen(QtGui.QColor("#ffe9a8"))
        painter.drawText(pot.adjusted(chip.right() - pot.left() + 4, 0, -6, 0),
                         QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, state["pot_text"])

    def _paint_seat(self, painter, seat, boxes, sizes):
        plate, cards = boxes["plate"], boxes["cards"]
        painter.setOpacity(0.45 if seat["folded"] else 1.0)
        if seat["winner_text"]:
            edge, edge_w = WINNER_EDGE, 3
        elif seat["acting"]:
            edge, edge_w = ACTOR_EDGE, 2
        elif seat["hero"]:
            edge, edge_w = HERO_EDGE, 2
        else:
            edge, edge_w = QtGui.QColor("#4a5764"), 1
        painter.setPen(QtGui.QPen(edge, edge_w))
        painter.setBrush(PLATE_FOLDED if seat["folded"] else PLATE)
        painter.drawRoundedRect(plate, 8, 8)

        badge = boxes["badge"]
        right = plate.right() - 6 - (badge.width() + 4 if badge else 0)
        name_font, sub_font = font(sizes["name_px"], True), font(sizes["sub_px"])
        label = seat["name"] + ("  %s" % seat["position"] if seat["position"] else "")
        available = right - plate.left() - 8
        painter.setFont(name_font)
        painter.setPen(QtGui.QColor("#e8eef4") if not seat["folded"] else MUTED)
        painter.drawText(QtCore.QRectF(plate.left() + 8, plate.top() + 2, available,
                                       plate.height() / 2),
                         QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                         QtGui.QFontMetricsF(name_font).elidedText(label, QtCore.Qt.ElideRight,
                                                                  available))
        status = seat["winner_text"] or seat["tag"]
        # Stack and tag share this line, and a stack shown in big blinds is wider than the same
        # money in dollars. The tag says something the stack cannot ('WINS 45 BB', 'ALL-IN'), so the
        # stack gives way to it and is elided, rather than the tag being dropped as it was while
        # both texts happened to fit.
        bold_sub = font(sizes["sub_px"], True)
        status_w = min(text_width(status, bold_sub), available * 0.6) if status else 0.0
        stack_w = max(0.0, min(text_width(seat["stack_text"], sub_font),
                               available - status_w - (6 if status else 0)))
        painter.setFont(sub_font)
        painter.setPen(MUTED)
        painter.drawText(QtCore.QRectF(plate.left() + 8, plate.center().y(), stack_w,
                                       plate.height() / 2),
                         QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                         QtGui.QFontMetricsF(sub_font).elidedText(seat["stack_text"],
                                                                  QtCore.Qt.ElideRight, stack_w))
        if status and status_w:
            painter.setFont(bold_sub)
            painter.setPen(WINNER_EDGE if seat["winner_text"] else RED)
            painter.drawText(QtCore.QRectF(plate.left() + 8 + stack_w + 4, plate.center().y(),
                                           available - stack_w - 4, plate.height() / 2),
                             QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                             QtGui.QFontMetricsF(bold_sub).elidedText(status,
                                                                      QtCore.Qt.ElideRight,
                                                                      status_w))

        if badge is not None:
            painter.setOpacity(1.0)
            painter.setPen(QtGui.QPen(QtGui.QColor("#c8c8c8"), 1))
            painter.setBrush(QtGui.QColor("#fafafa"))
            painter.drawEllipse(badge)
            painter.setPen(QtGui.QColor("#222222"))
            painter.setFont(font(badge.height() * 0.66, True))
            painter.drawText(badge, QtCore.Qt.AlignCenter, "D")

        card_h = int(cards.height())
        for index in range(len(seat["cards"])):
            rect = QtCore.QRectF(cards.left() + index * boxes["step"], cards.top(),
                                 cards.height() * CARD_RATIO, cards.height())
            image = (card_pixmap(seat["cards"][index], card_h) if seat["face_up"]
                     else card_back(card_h))
            painter.drawPixmap(rect.topLeft(), image)

        painter.setOpacity(1.0)
        if boxes["bet"] is not None:
            bet = boxes["bet"]
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(6, 12, 16, 225))
            painter.drawRoundedRect(bet, bet.height() / 2.0, bet.height() / 2.0)
            chip = chip_rect(bet)
            painter.setBrush(GOLD)
            painter.drawEllipse(chip)
            painter.setFont(font(sizes["bet_px"], True))
            painter.setPen(QtGui.QColor("#ffffff"))
            painter.drawText(bet.adjusted(chip.right() - bet.left() + 4, 0, -6, 0),
                             QtCore.Qt.AlignCenter, seat["bet_text"])
