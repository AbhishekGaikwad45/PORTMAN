#!/usr/bin/env python3
"""
Berth Layout Editor — standalone tool, independent of the PORTMAN web app.

Draw berth/vessel-area rectangles on top of static/img/Clean_berths.png,
drag them to move, dial in exact size/rotation, and export to
static/data/berth_layout.json. That JSON (center x/y, width, height, angle
in image-pixel space) is what a later RP01 port dashboard reads to plot
vessel icons on top of the image.

Run:   python tools/berth_layout_editor.py
Deps:  pip install PySide6   (PyQt6/PyQt5 also work)
Self-test (no Qt needed):  python tools/berth_layout_editor.py --selftest
"""
import json
import os
import sys
from dataclasses import asdict, dataclass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE_PATH = os.path.join(BASE_DIR, "static", "img", "Clean_berths.png")
JSON_PATH = os.path.join(BASE_DIR, "static", "data", "berth_layout.json")


@dataclass
class Berth:
    label: str
    cx: float
    cy: float
    w: float
    h: float
    angle: float = 0.0  # clockwise degrees, image pixel space

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return Berth(d["label"], float(d["cx"]), float(d["cy"]),
                     float(d["w"]), float(d["h"]), float(d.get("angle", 0.0)))


def normalize_rect(x0, y0, x1, y1):
    """Two arbitrary corners -> (center_x, center_y, width, height)."""
    x_min, x_max = sorted((x0, x1))
    y_min, y_max = sorted((y0, y1))
    w, h = x_max - x_min, y_max - y_min
    return x_min + w / 2, y_min + h / 2, w, h


def _selftest():
    assert normalize_rect(100, 200, 20, 50) == (60.0, 125.0, 80.0, 150.0)
    assert normalize_rect(20, 50, 100, 200) == (60.0, 125.0, 80.0, 150.0)

    b = Berth("BERTH 13", 512.4, 300.1, 120.0, 40.0, angle=27.5)
    assert Berth.from_dict(b.to_dict()) == b

    import tempfile
    payload = {"image": "Clean_berths.png", "image_size": [900, 1500], "berths": [b.to_dict()]}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "berth_layout.json")
        with open(path, "w") as f:
            json.dump(payload, f)
        with open(path) as f:
            loaded = json.load(f)
        assert Berth.from_dict(loaded["berths"][0]) == b

    print("selftest OK")


# ── Qt UI ────────────────────────────────────────────────────────────────
def run_gui():
    # ponytail: try bindings in popularity order so it runs on whatever's installed
    QtWidgets = QtCore = QtGui = None
    for mod in ("PySide6", "PyQt6", "PyQt5"):
        try:
            QtWidgets = __import__(mod + ".QtWidgets", fromlist=["x"])
            QtCore = __import__(mod + ".QtCore", fromlist=["x"])
            QtGui = __import__(mod + ".QtGui", fromlist=["x"])
            break
        except ImportError:
            continue
    if QtWidgets is None:
        sys.exit("No Qt binding found. Install one:  pip install PySide6")

    Qt = QtCore.Qt
    QRectF = QtCore.QRectF
    QShortcutCls = getattr(QtGui, "QShortcut", None) or QtWidgets.QShortcut  # Qt6 moved it to QtGui

    class BerthItem(QtWidgets.QGraphicsRectItem):
        def __init__(self, cx, cy, w, h, angle, label):
            super().__init__(-w / 2, -h / 2, w, h)
            self.setPos(cx, cy)
            self.setFlags(
                QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable
                | QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            )
            self.setPen(QtGui.QPen(QtGui.QColor("#facc15"), 2))
            self.setBrush(QtGui.QBrush(QtGui.QColor(250, 204, 21, 60)))
            self.label_item = QtWidgets.QGraphicsSimpleTextItem(label, self)
            font = self.label_item.font()
            font.setBold(True)
            font.setPointSize(9)
            self.label_item.setFont(font)
            self.label = label
            self.set_angle(angle)

        def _center_label(self):
            br = self.label_item.boundingRect()
            self.label_item.setPos(-br.width() / 2, -br.height() / 2)

        def set_angle(self, angle):
            self.setRotation(angle)
            self.label_item.setRotation(-angle)  # keep the text upright on screen
            self._center_label()

        def set_size(self, w, h):
            self.setRect(-w / 2, -h / 2, w, h)
            self._center_label()

        def set_label(self, text):
            self.label = text
            self.label_item.setText(text)
            self._center_label()

        def to_berth(self):
            r = self.rect()
            return Berth(self.label, self.pos().x(), self.pos().y(),
                         r.width(), r.height(), self.rotation())

    class DrawView(QtWidgets.QGraphicsView):
        """Left-drag draws a new rect when editor.add_mode is on; otherwise
        clicks pass through to Qt's native item select/move handling."""

        def __init__(self, scene, editor):
            super().__init__(scene)
            self.editor = editor
            self._start = None
            self._temp = None
            self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)

        def wheelEvent(self, event):
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.scale(factor, factor)

        def mousePressEvent(self, event):
            if self.editor.add_mode and event.button() == Qt.MouseButton.LeftButton:
                self._start = self.mapToScene(event.pos())
                self._temp = QtWidgets.QGraphicsRectItem(QRectF(self._start, self._start))
                self._temp.setPen(QtGui.QPen(QtGui.QColor("#22c55e"), 2, Qt.PenStyle.DashLine))
                self.scene().addItem(self._temp)
                return
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event):
            if self._temp is not None:
                self._temp.setRect(QRectF(self._start, self.mapToScene(event.pos())).normalized())
                return
            super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event):
            if self._temp is not None:
                r = self._temp.rect()
                self.scene().removeItem(self._temp)
                self._temp = None
                if r.width() > 3 and r.height() > 3:
                    self.editor.add_berth(r.x() + r.width() / 2, r.y() + r.height() / 2,
                                           r.width(), r.height())
                return
            super().mouseReleaseEvent(event)
            self.editor.refresh_panel()  # picks up drag-moves

    class Editor(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Berth Layout Editor")
            self.resize(1400, 900)
            self.add_mode = False
            self._current = None
            self._counter = 0

            self.scene = QtWidgets.QGraphicsScene(self)
            self.view = DrawView(self.scene, self)

            image_path = IMAGE_PATH
            if not os.path.isfile(image_path):
                image_path, _ = QtWidgets.QFileDialog.getOpenFileName(
                    self, "Choose port image", BASE_DIR, "Images (*.png *.jpg *.jpeg)")
            self.pixmap_item = QtWidgets.QGraphicsPixmapItem(QtGui.QPixmap(image_path))
            self.scene.addItem(self.pixmap_item)
            self.scene.setSceneRect(self.pixmap_item.boundingRect())

            self.list = QtWidgets.QListWidget()
            self.list.currentRowChanged.connect(self._select_row)

            self.label_edit = QtWidgets.QLineEdit()
            self.cx_spin = self._spin(-100000, 100000)
            self.cy_spin = self._spin(-100000, 100000)
            self.w_spin = self._spin(1, 100000)
            self.h_spin = self._spin(1, 100000)
            self.angle_spin = self._spin(-360, 360)
            for s in (self.cx_spin, self.cy_spin, self.w_spin, self.h_spin, self.angle_spin):
                s.valueChanged.connect(self._push_panel)
            self.label_edit.editingFinished.connect(self._push_panel)

            form = QtWidgets.QFormLayout()
            form.addRow("Label", self.label_edit)
            form.addRow("Center X", self.cx_spin)
            form.addRow("Center Y", self.cy_spin)
            form.addRow("Width", self.w_spin)
            form.addRow("Height", self.h_spin)
            form.addRow("Angle °", self.angle_spin)

            add_btn = QtWidgets.QPushButton("+ Add Berth (drag on image)")
            add_btn.setCheckable(True)
            add_btn.toggled.connect(lambda on: setattr(self, "add_mode", on))
            del_btn = QtWidgets.QPushButton("Delete Selected")
            del_btn.clicked.connect(self._delete_selected)
            save_btn = QtWidgets.QPushButton("Save JSON")
            save_btn.clicked.connect(self._save)

            side = QtWidgets.QWidget()
            side.setFixedWidth(300)
            v = QtWidgets.QVBoxLayout(side)
            v.addWidget(add_btn)
            v.addWidget(QtWidgets.QLabel("Berths"))
            v.addWidget(self.list, 1)
            v.addWidget(QtWidgets.QLabel("Selected berth"))
            v.addLayout(form)
            v.addWidget(del_btn)
            v.addWidget(save_btn)
            self.status = QtWidgets.QLabel("")
            self.status.setWordWrap(True)
            v.addWidget(self.status)

            central = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(central)
            h.addWidget(self.view, 1)
            h.addWidget(side)
            self.setCentralWidget(central)

            self.scene.selectionChanged.connect(self.refresh_panel)
            QShortcutCls(QtGui.QKeySequence(Qt.Key.Key_Delete), self, self._delete_selected)

            self._load_existing()

        def _spin(self, lo, hi):
            s = QtWidgets.QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setDecimals(1)
            return s

        def add_berth(self, cx, cy, w, h):
            self._counter += 1
            item = BerthItem(cx, cy, w, h, 0.0, f"BERTH {self._counter}")
            self._add_item_to_ui(item)
            self.scene.clearSelection()
            item.setSelected(True)

        def _add_item_to_ui(self, item):
            self.scene.addItem(item)
            li = QtWidgets.QListWidgetItem(item.label)
            li.setData(Qt.ItemDataRole.UserRole, item)
            self.list.addItem(li)

        def _row_for_item(self, item):
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.ItemDataRole.UserRole) is item:
                    return i
            return -1

        def _select_row(self, row):
            if row < 0:
                return
            item = self.list.item(row).data(Qt.ItemDataRole.UserRole)
            if item not in self.scene.selectedItems():
                self.scene.clearSelection()
                item.setSelected(True)
                self.view.centerOn(item)

        def refresh_panel(self):
            sel = [i for i in self.scene.selectedItems() if isinstance(i, BerthItem)]
            self._current = sel[0] if len(sel) == 1 else None
            fields = (self.label_edit, self.cx_spin, self.cy_spin,
                      self.w_spin, self.h_spin, self.angle_spin)
            for w in fields:
                w.blockSignals(True)
            if self._current:
                it, r = self._current, self._current.rect()
                self.label_edit.setText(it.label)
                self.cx_spin.setValue(it.pos().x())
                self.cy_spin.setValue(it.pos().y())
                self.w_spin.setValue(r.width())
                self.h_spin.setValue(r.height())
                self.angle_spin.setValue(it.rotation())
                row = self._row_for_item(it)
                if row != self.list.currentRow():
                    self.list.setCurrentRow(row)
            else:
                self.label_edit.setText("")
            for w in fields:
                w.blockSignals(False)

        def _push_panel(self):
            if self._current is None:
                return
            it = self._current
            it.set_label(self.label_edit.text() or it.label)
            it.setPos(self.cx_spin.value(), self.cy_spin.value())
            it.set_size(self.w_spin.value(), self.h_spin.value())
            it.set_angle(self.angle_spin.value())
            row = self._row_for_item(it)
            if row >= 0:
                self.list.item(row).setText(it.label)

        def _delete_selected(self):
            for item in list(self.scene.selectedItems()):
                if not isinstance(item, BerthItem):
                    continue
                row = self._row_for_item(item)
                if row >= 0:
                    self.list.takeItem(row)
                self.scene.removeItem(item)
            self._current = None
            self.refresh_panel()

        def _load_existing(self):
            if not os.path.isfile(JSON_PATH):
                return
            with open(JSON_PATH) as f:
                data = json.load(f)
            for d in data.get("berths", []):
                b = Berth.from_dict(d)
                self._counter += 1
                self._add_item_to_ui(BerthItem(b.cx, b.cy, b.w, b.h, b.angle, b.label))
            self.status.setText(f"Loaded {len(data.get('berths', []))} berths from {JSON_PATH}")

        def _save(self):
            berths = [self.list.item(i).data(Qt.ItemDataRole.UserRole).to_berth().to_dict()
                      for i in range(self.list.count())]
            payload = {
                "image": os.path.basename(IMAGE_PATH),
                "image_size": [self.pixmap_item.pixmap().width(), self.pixmap_item.pixmap().height()],
                "berths": berths,
            }
            os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
            with open(JSON_PATH, "w") as f:
                json.dump(payload, f, indent=2)
            self.status.setText(f"Saved {len(berths)} berths to {JSON_PATH}")

    app = QtWidgets.QApplication(sys.argv)
    win = Editor()
    win.show()
    sys.exit(app.exec() if hasattr(app, "exec") else app.exec_())


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        run_gui()
