import os
import traceback

import binaryninja as bn
import binaryninjaui

try:
    import PySide6
except:
    import PySide2 as PySide6

from PySide6.QtGui import QColor
from PySide6.QtCore import Qt, QAbstractItemModel, QModelIndex
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QTreeView, QMenu

import sqlite3
from .binexport import binexport2_pb2

def match_get_metadata(db):
	c = db.cursor()
	c.execute("SELECT * FROM metadata")
	metadata = dict(c.fetchone())
	assert(c.fetchone() == None)
	return metadata

def match_get_algorithm_names(db, table_name):
	c = db.cursor()
	c.execute("SELECT * FROM {}".format(table_name))

	result = {}
	for row in c.fetchall():
		result[row["id"]] = row["name"]
	return result

def binexport_get_names(be):
	cg = be.call_graph
	if not cg:
		return {}

	name_mapping = {}
	for v in cg.vertex:
		if v.address and v.mangled_name:
			name_mapping[v.address] = v.mangled_name
	return name_mapping


class BindiffViewerDialog(QDialog):
	def __init__(self, bv, match_db, role, primary_be, secondary_be):
		super(BindiffViewerDialog, self).__init__()

		self.bv = bv
		self.primary_be = primary_be
		self.secondary_be = secondary_be
		self.role = role

		# UI
		self.match_model = BindiffMatchModel(bv, match_db, role, primary_be, secondary_be)

		self.match_view = QTreeView()
		self.match_view.setModel(self.match_model)

		self.match_view.setSelectionMode(QTreeView.ExtendedSelection)

		self.match_view.setContextMenuPolicy(Qt.CustomContextMenu)
		self.match_view.customContextMenuRequested.connect(self.match_view_context_menu_requested)
		self.match_view.doubleClicked.connect(self.match_view_double_clicked)

		self.match_view.setRootIsDecorated(False)
		self.match_view.setFont(binaryninjaui.getMonospaceFont(self))

		for i in range(len(self.match_model.column_infos)):
			self.match_view.resizeColumnToContents(i)

		self.match_view.setSortingEnabled(True)
		self.match_view.sortByColumn(0, Qt.AscendingOrder)

		layout = QVBoxLayout()
		layout.addWidget(self.match_view)

		self.setLayout(layout)
		self.setWindowTitle("BinDiff Viewer")
		self.resize(1000, 640)
		flags = self.windowFlags()
		flags |= Qt.WindowMaximizeButtonHint
		flags &= ~Qt.WindowContextHelpButtonHint
		self.setWindowFlags(flags)

	def match_view_double_clicked(self, index):
		if not index.isValid():
			assert(False)
			return
		if self.role == None:
			return

		entry = self.match_model.entries[index.row()]
		if self.role == 0:
			address = entry["address1"]
		elif self.role == 1:
			address = entry["address2"]
		else:
			assert(False)

		self.bv.navigate(self.bv.file.view, address)

	def match_view_context_menu_requested(self, pos):
		if self.role == None:
			return

		selected_indices = self.match_view.selectionModel().selectedIndexes()

		# This may return each row multiple times, so we uniquify
		selected = set([i.row() for i in selected_indices])

		def action_port_symbols():
			for i in selected:
				self.port_symbols(i)

		menu = QMenu(self.match_view)
		menu.addAction("Port symbols", action_port_symbols)
		menu.exec_(self.match_view.mapToGlobal(pos))

	def port_symbols(self, i):
		if self.role == None:
			return

		entry = self.match_model.entries[i]
		target_index = self.role
		source_index = 1 if target_index == 0 else 0

		source_name = entry["name{}".format(source_index + 1)]
		target_address = entry["address{}".format(target_index + 1)]

		old_sym = self.bv.get_symbol_at(target_address)

		target_name = None
		if old_sym:
			target_name = old_sym.name
		target_text = target_name if target_name else "<unnamed>"

		if not source_name:
			bn.log_warn("Port symbols: {} @ {:x} has no source name, skipping".format(target_text, target_address))
			return

		if old_sym and not old_sym.auto:
			bn.log_warn("Port symbols: {} @ {:x} is already named, skipping".format(target_text, target_address))
			return

		bn.log_info("Port symbols: {} @ {:x} -> {}".format(target_text, target_address, source_name))
		new_sym = bn.Symbol(bn.SymbolType.FunctionSymbol, target_address, source_name)
		self.bv.define_user_symbol(new_sym)

class BindiffMatchModel(QAbstractItemModel):
	def __init__(self, bv, match_db, role, primary_be, secondary_be):
		super(BindiffMatchModel, self).__init__()

		self.metadata = match_get_metadata(match_db)
		self.function_algorithm_names = match_get_algorithm_names(match_db, "functionalgorithm")
		self.role = role

		def col_field(key, default=None):
			def f(i):
				entry = self.entries[i]
				result = entry[key]
				if result == None:
					return default
				return result
			return f

		def col_field_fmt(key, fmt):
			return lambda i: fmt.format(self.entries[i][key])

		def col_addr_field(key):
			return lambda i: "{:x}".format(self.entries[i][key])

		# Column name, sort key, display function
		self.column_infos = [
			("Similarity", "similarity", col_field_fmt("similarity", "{:.2f}")),
			("Confidence", "confidence", col_field_fmt("confidence", "{:.2f}")),
			("P Address",  "address1",   col_addr_field("address1")),
			("P Name",     "name1",      col_field("name1")),
			("S Address",  "address2",   col_addr_field("address2")),
			("S Name",     "name2",      col_field("name2")),
			("Algorithm",  "algorithm",  lambda i: self.function_algorithm_names[self.entries[i]["algorithm"]]),
		]

		# Extract function names
		file_names = [
			binexport_get_names(primary_be),
			binexport_get_names(secondary_be)
		]

		# Load matches (big query)
		self.entries = []
		c = match_db.cursor()
		c.execute("SELECT * FROM function")
		rows = c.fetchall()

		# Roll into table
		for row in rows:
			entry = dict(row)

			# Add name information
			def get_name(file_role, address):
				if file_role == self.role:
					sym = bv.get_symbol_at(address)
					if not sym or sym.auto:
						return ""
					return sym.name
				return file_names[file_role].get(address, "")

			entry["name1"] = get_name(0, entry["address1"])
			entry["name2"] = get_name(1, entry["address2"])

			self.entries.append(entry)

	def index(self, row, col, parent):
		if parent.isValid():
			# No children
			return QModelIndex()

		if row >= len(self.entries):
			return QModelIndex()
		if col >= len(self.column_infos):
			return QModelIndex()

		return self.createIndex(row, col)

	def parent(self, index):
		# Flat tree, no parent
		return QModelIndex()

	def rowCount(self, parent):
		# No children
		if parent.isValid():
			return 0
		return len(self.entries)

	def columnCount(self, parent):
		return len(self.column_infos)

	def data(self, index, role):
		if index.row() >= len(self.entries):
			return None

		name, key, display = self.column_infos[index.column()]
		if role == Qt.DisplayRole:
			return display(index.row())
		elif role == Qt.BackgroundRole:
			if name == "Confidence" or name == "Similarity":
				value = display(index.row())

				ramp_start_hue = 14
				ramp_end_hue = 88
				hue = ramp_start_hue + float(value) * (ramp_end_hue - ramp_start_hue)

				color = QColor()
				color.setHsv(hue, 204, 153)
				return color

		return None

	def headerData(self, section, orientation, role):
		if role != Qt.DisplayRole:
			return None
		if orientation != Qt.Horizontal:
			return None

		name, key, display = self.column_infos[section]
		return name

	def sort(self, col, order):
		self.beginResetModel()

		name, key, display = self.column_infos[col]
		self.entries.sort(
			key=lambda k: k[key],
			reverse=(order != Qt.AscendingOrder)
		)

		self.endResetModel()

def view_bindiff_matches(bv, match_db_path, role):
	def sqlite_open_ro(path):
		db = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
		db.row_factory = sqlite3.Row
		return db

	# Load main DB
	match_db = sqlite_open_ro(match_db_path)
	match_cursor = match_db.cursor()

	# Get metadata
	match_metadata = match_get_metadata(match_db)

	# Get file info
	def get_match_file_info(file_id):
		match_cursor.execute("SELECT * FROM file WHERE id=?", (file_id,))
		result = dict(match_cursor.fetchone())
		assert(match_cursor.fetchone() == None)
		return result

	match_primary_file = get_match_file_info(match_metadata["file1"])
	match_secondary_file = get_match_file_info(match_metadata["file2"])

	# Load export DBs
	def load_binexport_by_name(name):
		pb = binexport2_pb2.BinExport2()
		binexport_path = os.path.join(
			os.path.dirname(match_db_path),
			name + ".BinExport"
		)
		with open(binexport_path, "rb") as f:
			pb.ParseFromString(f.read())
		return pb

	primary_be = load_binexport_by_name(match_primary_file["filename"])
	secondary_be = load_binexport_by_name(match_secondary_file["filename"])

	# Qt
	assert(QApplication.instance() != None)

	global dialog
	dialog = BindiffViewerDialog(bv, match_db, role, primary_be, secondary_be)
	dialog.show()
	dialog.raise_()
	dialog.activateWindow()

def dialog(bv):
	match_path_field = bn.OpenFileNameField("Match File", "*.BinDiff")
	role_field = bn.ChoiceField(
		"Current view role",
		[ "None", "Primary", "Secondary" ]
	)

	form_fields = [
		match_path_field,
		role_field
	]

	# Present form
	if not bn.get_form_input(form_fields, "BinDiff Viewer"):
		# User cancelled
		return

	match_path = match_path_field.result
	if role_field.result == 0:
		role = None
	else:
		role = role_field.result - 1
	try:
		view_bindiff_matches(bv, match_path, role)
	except:
		bn.show_message_box(
			"BinDiff Viewer Error",
			"Failed to load matches:\n{}".format(traceback.format_exc())
		)

bn.PluginCommand.register(
	"BinDiff Viewer",
	"View BinDiff results",
	dialog
)
