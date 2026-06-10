"""Diálogo de generación del reporte 'Dispensas por Equipo' (PDF + Excel).

Deja elegir el ALCANCE del reporte —todos los equipos, equipos específicos
(lista con buscador y casillas), o una dimensión del maestro (categoría, grupo,
departamento, marca, cost centre) con un valor concreto—, el rango de fechas y
los formatos de salida. La lectura de la réplica, la clasificación contra el
SFL y el dibujo del PDF corren en un `QThread` (`_ReportWorker`): la interfaz
nunca se congela y muestra el avance página a página, con cancelación limpia.

Se abre desde la ventana de auditoría SFL (su dominio natural: el reporte ES la
clasificación de despachos contra el Safe Fill Level).
"""
from __future__ import annotations

import os
import threading
import traceback

import pandas as pd
from PySide6.QtCore import QDate, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QFileDialog, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QProgressBar, QPushButton, QVBoxLayout,
)

from msgq.core import dispense_report as dr
from msgq.i18n import t
from msgq.storage import Database

_RANGES = (
    ("Últimos 7 días", 7),
    ("Últimos 30 días", 30),
    ("Últimos 90 días", 90),
    ("Últimos 12 meses", 365),
    ("Todo el rango", None),
)
_HISTORY_START = pd.Timestamp("2022-01-01")

# Columnas de `movements` que el reporte consume (ver core/dispense_report).
_MOVEMENT_COLS = ["id", "kind", "record_collected_at", "updated_at",
                  "equipment_id", "equipment_description", "product",
                  "volume", "field_user", "tank"]


class _ReportWorker(QThread):
    """Lee la réplica, clasifica y exporta (PDF/Excel) fuera del hilo de la GUI."""

    progress = Signal(int, int)          # (página escrita, total de páginas)
    done = Signal(dict)                  # {"pdf": path|None, "xlsx": path|None,
    #                                       "kpis": dict, "cancelled": bool}
    failed = Signal(str)

    def __init__(self, db_path: str, *, date_from, date_to, equipment_ids,
                 dimension, value, scope_label, pdf_path, xlsx_path, parent=None):
        super().__init__(parent)
        self._db_path = db_path
        self._from = date_from
        self._to = date_to
        self._ids = equipment_ids
        self._dim = dimension
        self._value = value
        self._scope_label = scope_label
        self._pdf_path = pdf_path
        self._xlsx_path = xlsx_path
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:  # noqa: D401 - QThread entrypoint
        try:
            rdb = Database(self._db_path, create=False)
            try:
                movements = rdb.read("movements", columns=_MOVEMENT_COLS)
                equipment = rdb.get_equipment()
                limits = rdb.get_consumption_limits()
            finally:
                rdb.close()

            dataset = dr.build_dataset(movements, equipment, limits,
                                       self._from, self._to)
            scoped = dr.filter_scope(dataset, equipment_ids=self._ids,
                                     dimension=self._dim, value=self._value)

            # Equipos elegidos explicitamente SIN despachos en el rango: van al
            # PDF con el rotulo "Sin despachos en el rango" (como la muestra).
            extra: list[tuple[str, str]] = []
            if self._ids:
                have = (set(scoped["equipment_id"].astype(str))
                        if not scoped.empty else set())
                desc_by_id = {}
                if equipment is not None and not equipment.empty:
                    e = equipment.drop_duplicates("equipment_id")
                    desc_by_id = dict(zip(e["equipment_id"].astype(str),
                                          e["description"]))
                for eq in self._ids:
                    if str(eq) not in have:
                        d = desc_by_id.get(str(eq))
                        extra.append((str(eq), "" if pd.isna(d) else str(d or "")))

            out = {"pdf": None, "xlsx": None, "kpis": dr.overall_kpis(scoped),
                   "cancelled": False}
            if self._pdf_path:
                from msgq.export.dispense_report import export_pdf
                export_pdf(self._pdf_path, scoped, scope_label=self._scope_label,
                           extra_equipment=extra,
                           progress=lambda p, n: self.progress.emit(p, n),
                           cancel=self._cancel.is_set)
                if self._cancel.is_set():
                    # Generacion interrumpida: el PDF parcial no debe quedar
                    # como si fuera un reporte completo.
                    try:
                        os.remove(self._pdf_path)
                    except OSError:
                        pass
                    out["cancelled"] = True
                    self.done.emit(out)
                    return
                out["pdf"] = self._pdf_path
            if self._xlsx_path and not self._cancel.is_set():
                from msgq.export.dispense_report import export_excel
                export_excel(self._xlsx_path, scoped,
                             scope_label=self._scope_label)
                out["xlsx"] = self._xlsx_path
            out["cancelled"] = self._cancel.is_set()
            self.done.emit(out)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class DispenseReportDialog(QDialog):
    """Configura y genera el reporte 'Dispensas por Equipo'."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self._db = db
        self._worker: _ReportWorker | None = None
        self._loading_range = False
        self.setWindowTitle(t("Reporte: Dispensas por Equipo"))
        self.resize(640, 620)
        try:
            self._equipment = db.get_equipment()
        except Exception:  # noqa: BLE001
            self._equipment = pd.DataFrame()
        self._build()

    # --- construcción --------------------------------------------------------

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        # Alcance ------------------------------------------------------------
        box_scope = QGroupBox(t("Alcance"))
        gs = QGridLayout(box_scope)
        self.cmb_scope = QComboBox()
        self.cmb_scope.addItem(t("Todos los equipos"), None)
        self.cmb_scope.addItem(t("Equipos específicos"), "ids")
        for col, label in dr.SCOPE_DIMENSIONS:
            self.cmb_scope.addItem(f"{t('Por')} {t(label).lower()}", col)
        self.cmb_scope.currentIndexChanged.connect(self._on_scope_changed)
        gs.addWidget(QLabel(t("Generar para:")), 0, 0)
        gs.addWidget(self.cmb_scope, 0, 1)

        self.cmb_value = QComboBox()          # valor de la dimensión elegida
        self.lbl_value = QLabel(t("Valor:"))
        gs.addWidget(self.lbl_value, 1, 0)
        gs.addWidget(self.cmb_value, 1, 1)

        self.txt_filter = QLineEdit()         # buscador de la lista de equipos
        self.txt_filter.setPlaceholderText(t("Filtrar equipos por ID o descripción…"))
        self.txt_filter.textChanged.connect(self._filter_equipment_list)
        self.lst_equipment = QListWidget()
        self.lst_equipment.setSelectionMode(QListWidget.NoSelection)
        self._populate_equipment_list()
        gs.addWidget(self.txt_filter, 2, 0, 1, 2)
        gs.addWidget(self.lst_equipment, 3, 0, 1, 2)
        lay.addWidget(box_scope)

        # Rango de fechas ------------------------------------------------------
        box_range = QGroupBox(t("Rango de fechas"))
        gr = QHBoxLayout(box_range)
        self.cmb_range = QComboBox()
        for label, days in _RANGES:
            self.cmb_range.addItem(t(label), days)
        self.cmb_range.setCurrentIndex(2)     # Últimos 90 días
        self.cmb_range.currentIndexChanged.connect(self._apply_quick_range)
        today = QDate.currentDate()
        self.date_from = QDateEdit(today.addDays(-90))
        self.date_to = QDateEdit(today)
        for de in (self.date_from, self.date_to):
            de.setDisplayFormat("dd/MM/yyyy")
            de.setCalendarPopup(True)
        gr.addWidget(self.cmb_range)
        gr.addWidget(QLabel(t("Desde:")))
        gr.addWidget(self.date_from)
        gr.addWidget(QLabel(t("Hasta:")))
        gr.addWidget(self.date_to)
        gr.addStretch(1)
        lay.addWidget(box_range)

        # Formatos -------------------------------------------------------------
        box_fmt = QGroupBox(t("Formato de salida"))
        gf = QHBoxLayout(box_fmt)
        self.chk_pdf = QCheckBox(t("PDF (gráficas por equipo)"))
        self.chk_pdf.setChecked(True)
        self.chk_xlsx = QCheckBox(t("Excel (tablas analíticas)"))
        self.chk_xlsx.setChecked(True)
        gf.addWidget(self.chk_pdf)
        gf.addWidget(self.chk_xlsx)
        gf.addStretch(1)
        lay.addWidget(box_fmt)

        # Progreso + acciones ---------------------------------------------------
        self.prg = QProgressBar()
        self.prg.setRange(0, 100)
        self.prg.setValue(0)
        self.prg.setTextVisible(True)
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color:#5A6B7B;")
        lay.addWidget(self.prg)
        lay.addWidget(self.lbl_status)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_generate = QPushButton(t("Generar…"))
        self.btn_generate.setObjectName("accent")
        self.btn_generate.clicked.connect(self._on_generate)
        self.btn_cancel = QPushButton(t("Cancelar generación"))
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._on_cancel)
        btn_close = QPushButton(t("Cerrar"))
        btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_generate)
        row.addWidget(self.btn_cancel)
        row.addWidget(btn_close)
        lay.addLayout(row)

        self._on_scope_changed()

    def _populate_equipment_list(self) -> None:
        self.lst_equipment.clear()
        if self._equipment is None or self._equipment.empty:
            return
        e = self._equipment.drop_duplicates("equipment_id").sort_values("equipment_id")
        for _, r in e.iterrows():
            eq_id = r.get("equipment_id")
            if eq_id is None or pd.isna(eq_id):
                continue
            desc = r.get("description")
            desc = "" if desc is None or pd.isna(desc) else str(desc)
            item = QListWidgetItem(f"{eq_id} — {desc}"[:90])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, str(eq_id))
            self.lst_equipment.addItem(item)

    def _filter_equipment_list(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.lst_equipment.count()):
            it = self.lst_equipment.item(i)
            it.setHidden(bool(needle) and needle not in it.text().lower())

    def _on_scope_changed(self) -> None:
        scope = self.cmb_scope.currentData()
        is_ids = scope == "ids"
        is_dim = scope not in (None, "ids")
        self.txt_filter.setVisible(is_ids)
        self.lst_equipment.setVisible(is_ids)
        self.lbl_value.setVisible(is_dim)
        self.cmb_value.setVisible(is_dim)
        if is_dim:
            self.cmb_value.clear()
            if self._equipment is not None and scope in self._equipment.columns:
                vals = (self._equipment[scope].astype("string").str.strip()
                        .replace({"": pd.NA}).dropna().unique())
                for v in sorted(vals):
                    self.cmb_value.addItem(str(v), str(v))
        self.adjustSize()

    # --- rango ---------------------------------------------------------------

    def _apply_quick_range(self) -> None:
        days = self.cmb_range.currentData()
        today = QDate.currentDate()
        if days is None:
            self.date_from.setDate(QDate(_HISTORY_START.year, _HISTORY_START.month,
                                         _HISTORY_START.day))
        else:
            self.date_from.setDate(today.addDays(-int(days)))
        self.date_to.setDate(today)

    # --- generación ------------------------------------------------------------

    def _scope_params(self) -> tuple[list[str] | None, str | None, str | None, str]:
        """(equipment_ids, dimension, value, etiqueta_de_alcance) según la UI."""
        scope = self.cmb_scope.currentData()
        if scope == "ids":
            ids = [self.lst_equipment.item(i).data(Qt.UserRole)
                   for i in range(self.lst_equipment.count())
                   if self.lst_equipment.item(i).checkState() == Qt.Checked]
            label = (", ".join(ids[:3]) + ("…" if len(ids) > 3 else "")
                     if ids else "")
            return ids, None, None, f"{t('Equipos')}: {label} ({len(ids)})"
        if scope is not None:
            value = self.cmb_value.currentData()
            dim_label = dict(dr.SCOPE_DIMENSIONS).get(scope, scope)
            return None, scope, value, f"{t(dim_label)}: {value}"
        return None, None, None, t("Todos los equipos")

    def _on_generate(self) -> None:
        ids, dim, value, scope_label = self._scope_params()
        if self.cmb_scope.currentData() == "ids" and not ids:
            QMessageBox.warning(self, t("Sin equipos"),
                                t("Marca al menos un equipo de la lista."))
            return
        if dim is not None and value is None:
            QMessageBox.warning(self, t("Sin valor"),
                                t("Elige un valor para la dimensión."))
            return
        if not self.chk_pdf.isChecked() and not self.chk_xlsx.isChecked():
            QMessageBox.warning(self, t("Sin formato"),
                                t("Marca al menos un formato (PDF o Excel)."))
            return

        d_from = pd.Timestamp(self.date_from.date().toPython())
        d_to = (pd.Timestamp(self.date_to.date().toPython()).normalize()
                + pd.Timedelta(days=1))
        stamp = f"{self.date_from.date().toString('ddMMyyyy')}-{self.date_to.date().toString('ddMMyyyy')}"
        scope_label = f"{scope_label} · {self.date_from.date().toString('dd/MM/yyyy')}–{self.date_to.date().toString('dd/MM/yyyy')}"

        pdf_path = xlsx_path = None
        if self.chk_pdf.isChecked():
            pdf_path, _ = QFileDialog.getSaveFileName(
                self, t("Guardar PDF"),
                f"Dispensas_por_Equipo_{stamp}.pdf", "PDF (*.pdf)")
            if not pdf_path:
                return
        if self.chk_xlsx.isChecked():
            xlsx_path, _ = QFileDialog.getSaveFileName(
                self, t("Guardar Excel"),
                f"Dispensas_por_Equipo_{stamp}.xlsx", "Excel (*.xlsx)")
            if not xlsx_path:
                return

        self._set_running(True)
        self.lbl_status.setText(t("Leyendo réplica y clasificando despachos…"))
        self.prg.setRange(0, 0)              # indeterminado durante la lectura
        self._worker = _ReportWorker(
            self._db.path, date_from=d_from, date_to=d_to, equipment_ids=ids,
            dimension=dim, value=value, scope_label=scope_label,
            pdf_path=pdf_path, xlsx_path=xlsx_path, parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _set_running(self, running: bool) -> None:
        self.btn_generate.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        for w in (self.cmb_scope, self.cmb_value, self.txt_filter,
                  self.lst_equipment, self.cmb_range, self.date_from,
                  self.date_to, self.chk_pdf, self.chk_xlsx):
            w.setEnabled(not running)

    def _on_progress(self, page: int, total: int) -> None:
        self.prg.setRange(0, max(1, total))
        self.prg.setValue(page)
        self.lbl_status.setText(
            f"{t('Generando PDF…')} {t('página')} {page} {t('de')} {total}")

    def _on_done(self, out: dict) -> None:
        self._worker = None
        self._set_running(False)
        self.prg.setRange(0, 100)
        if out.get("cancelled"):
            self.prg.setValue(0)
            self.lbl_status.setText(t("Generación cancelada."))
            return
        self.prg.setValue(100)
        k = out.get("kpis", {})
        self.lbl_status.setText(
            f"✓ {k.get('Equipos', 0):,} {t('equipos')} · "
            f"{k.get('Despachos', 0):,} {t('despachos')} · "
            f"{t('Normal')}: {k.get('Normal', 0):,} · "
            f"{t('Over SFL')}: {k.get('Over SFL', 0):,}")
        files = "\n".join(p for p in (out.get("pdf"), out.get("xlsx")) if p)
        QMessageBox.information(self, t("Reporte generado"),
                                f"{t('Reporte generado:')}\n{files}")

    def _on_failed(self, message: str) -> None:
        self._worker = None
        self._set_running(False)
        self.prg.setRange(0, 100)
        self.prg.setValue(0)
        self.lbl_status.setText("")
        QMessageBox.critical(self, t("Error al generar"), message)

    def _on_cancel(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self.lbl_status.setText(t("Cancelando…"))

    def closeEvent(self, event):  # noqa: N802 - override Qt
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
        event.accept()
