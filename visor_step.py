"""STEP Portable v2 — Visor STEP con nombres, ensamblajes y control de visibilidad.

Abre archivos STEP/STP con Open CASCADE (XCAF) y los dibuja con VTK.
Si un archivo no aporta nombres, asigna identificadores Cuerpo 001, ...
Este archivo es independiente para facilitar actualizarlo desde GitHub.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from OCP.BRep import BRep_Tool
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.STEPControl import STEPControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.TopLoc import TopLoc_Location
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDockWidget, QFileDialog, QLabel,
    QMainWindow, QMessageBox, QStatusBar, QToolBar, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
from vtkmodules.vtkCommonCore import vtkPoints
from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
from vtkmodules.vtkFiltersCore import vtkPolyDataNormals
from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera
from vtkmodules.vtkRenderingAnnotation import vtkAxesActor
from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper, vtkPropPicker, vtkRenderer
from vtkmodules.vtkInteractionWidgets import vtkOrientationMarkerWidget
import vtkmodules.vtkRenderingFreeType  # noqa: F401 - plugin VTK
import vtkmodules.vtkRenderingOpenGL2  # noqa: F401 - plugin VTK


APP_NAME = 'STEP Portable v2 · Componentes'
NORMAL_COLOR = (0.53, 0.77, 0.96)
SELECTED_COLOR = (1.0, 0.64, 0.18)


@dataclass
class ModelNode:
    """Componente o cuerpo; los nodos hoja contienen una malla VTK."""
    name: str
    children: list['ModelNode'] = field(default_factory=list)
    poly: Any = None


def label_name(label: TDF_Label) -> str:
    """Obtiene el nombre XCAF Unicode sin inventar un nombre si falta."""
    name_attr = TDataStd_Name()
    if label.IsNull() or not label.FindAttribute(TDataStd_Name.GetID_s(), name_attr):
        return ''
    name = name_attr.Get().ToExtString().strip()
    # Algunos exportadores STEP guardan UTF-8 interpretado como Latin-1.
    if 'Ã' in name or 'Â' in name:
        try:
            name = name.encode('latin-1').decode('utf-8')
        except UnicodeError:
            pass
    # Nombres técnicos generados por el lector, no por el diseñador.
    if name.upper() in {'SOLID', 'SHELL', 'COMPOUND', 'ASSEMBLY', 'SHAPE'}:
        return ''
    if name.startswith('=>[') or name.startswith('Open CASCADE STEP translator '):
        return ''
    return name


def shape_solids(shape: Any) -> list[Any]:
    found = []
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    while explorer.More():
        found.append(explorer.Current())
        explorer.Next()
    return found


def mesh_of_shape(shape: Any) -> Any:
    """Convierte caras B-Rep a VTK, respetando posiciones de instancia."""
    BRepMesh_IncrementalMesh(shape, 0.25, False, 0.35, True)
    points, triangles = vtkPoints(), vtkCellArray()
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is not None:
            offset = points.GetNumberOfPoints()
            transform = location.Transformation()
            for j in range(1, triangulation.NbNodes() + 1):
                p = triangulation.Node(j).Transformed(transform)
                points.InsertNextPoint(p.X(), p.Y(), p.Z())
            reverse = face.Orientation() == TopAbs_REVERSED
            for j in range(1, triangulation.NbTriangles() + 1):
                a, b, c = triangulation.Triangle(j).Get()
                if reverse:
                    b, c = c, b
                triangles.InsertNextCell(3)
                for index in (a, b, c):
                    triangles.InsertCellPoint(offset + index - 1)
        explorer.Next()

    if triangles.GetNumberOfCells() == 0:
        return None
    poly = vtkPolyData()
    poly.SetPoints(points)
    poly.SetPolys(triangles)
    normals = vtkPolyDataNormals()
    normals.SetInputData(poly)
    normals.SetFeatureAngle(55)
    normals.SplittingOff()
    normals.ConsistencyOn()
    normals.Update()
    output = vtkPolyData()
    output.ShallowCopy(normals.GetOutput())
    return output


def read_step_xcaf(path: str) -> list[ModelNode]:
    """Lee estructura XCAF: nombres, referencias y ensamblajes anidados."""
    doc = TDocStd_Document(TCollection_ExtendedString('XmlXCAF'))
    app = XCAFApp_Application.GetApplication_s()
    app.NewDocument(TCollection_ExtendedString('XmlXCAF'), doc)
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    if reader.ReadFile(str(path)) != IFSelect_RetDone or not reader.Transfer(doc):
        raise ValueError('No se pudo importar el archivo STEP.')
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    roots = TDF_LabelSequence()
    tool.GetFreeShapes(roots)
    if roots.Length() == 0:
        raise ValueError('El STEP no tiene cuerpos para visualizar.')

    body_number = 0
    assembly_number = 0

    def body_fallback() -> str:
        nonlocal body_number
        body_number += 1
        return f'Cuerpo {body_number:03d}'

    def named_subshape(definition: TDF_Label, raw_solid: Any) -> str:
        sub_label = TDF_Label()
        if tool.FindSubShape(definition, raw_solid, sub_label):
            return label_name(sub_label)
        return ''

    def walk(label: TDF_Label, location: TopLoc_Location,
             override_name: str = '', depth: int = 0) -> ModelNode | None:
        nonlocal assembly_number
        if depth > 64:
            raise ValueError('El archivo tiene una jerarquía de componentes demasiado profunda.')

        if XCAFDoc_ShapeTool.IsReference_s(label):
            referred = TDF_Label()
            if XCAFDoc_ShapeTool.GetReferredShape_s(label, referred):
                instance_name = label_name(label) or override_name
                instance_location = XCAFDoc_ShapeTool.GetLocation_s(label)
                return walk(referred, location.Multiplied(instance_location),
                            instance_name, depth + 1)

        display_name = override_name or label_name(label)
        if XCAFDoc_ShapeTool.IsAssembly_s(label):
            labels = TDF_LabelSequence()
            XCAFDoc_ShapeTool.GetComponents_s(label, labels, False)
            children = []
            for i in range(1, labels.Length() + 1):
                child = walk(labels.Value(i), location, depth=depth + 1)
                if child is not None:
                    children.append(child)
            if not children:
                return None
            if not display_name:
                assembly_number += 1
                display_name = f'Conjunto {assembly_number:03d}'
            return ModelNode(display_name, children=children)

        definition_shape = XCAFDoc_ShapeTool.GetShape_s(label)
        if definition_shape.IsNull():
            return None
        solids = shape_solids(definition_shape)
        if len(solids) == 1:
            raw = solids[0]
            name = display_name or named_subshape(label, raw) or body_fallback()
            poly = mesh_of_shape(raw.Moved(location))
            return ModelNode(name, poly=poly) if poly is not None else None
        if len(solids) > 1:
            children = []
            for raw in solids:
                name = named_subshape(label, raw) or body_fallback()
                poly = mesh_of_shape(raw.Moved(location))
                if poly is not None:
                    children.append(ModelNode(name, poly=poly))
            if not children:
                return None
            if not display_name:
                assembly_number += 1
                display_name = f'Pieza {assembly_number:03d}'
            return ModelNode(display_name, children=children)

        # STEPs de superficies (sin sólidos cerrados): un cuerpo visualizable.
        poly = mesh_of_shape(definition_shape.Moved(location))
        if poly is None:
            return None
        return ModelNode(display_name or body_fallback(), poly=poly)

    nodes = []
    for i in range(1, roots.Length() + 1):
        node = walk(roots.Value(i), TopLoc_Location())
        if node is not None:
            nodes.append(node)
    if not nodes:
        raise ValueError('No se pudo generar una malla de este archivo STEP.')
    return nodes


def read_step_fallback(path: str) -> list[ModelNode]:
    """Importación geométrica de respaldo para STEP sin estructura XCAF."""
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone or reader.TransferRoots() < 1:
        raise ValueError('No se pudo leer la geometría STEP.')
    shape = reader.OneShape()
    if shape.IsNull():
        raise ValueError('El STEP está vacío.')
    solids = shape_solids(shape)
    candidates = solids if solids else [shape]
    nodes = []
    for raw in candidates:
        poly = mesh_of_shape(raw)
        if poly is not None:
            nodes.append(ModelNode(f'Cuerpo {len(nodes) + 1:03d}', poly=poly))
    if not nodes:
        raise ValueError('No se encontraron caras para dibujar.')
    return nodes


def read_step_model(path: str) -> list[ModelNode]:
    try:
        return read_step_xcaf(path)
    except Exception as metadata_error:
        try:
            return read_step_fallback(path)
        except Exception as geometry_error:
            raise ValueError(f'No se pudo abrir el STEP: {geometry_error}') from metadata_error


class StepViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 800)
        self.setAcceptDrops(True)
        self.actors: list[Any] = []
        self.leaf_items: list[QTreeWidgetItem] = []
        self.wireframe = False

        center = QWidget(self)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        self.vtk_widget = QVTKRenderWindowInteractor(center)
        center_layout.addWidget(self.vtk_widget)
        self.setCentralWidget(center)

        self.renderer = vtkRenderer()
        self.renderer.SetBackground(0.075, 0.094, 0.13)
        self.renderer.SetBackground2(0.15, 0.20, 0.27)
        self.renderer.GradientBackgroundOn()
        self.vtk_widget.GetRenderWindow().AddRenderer(self.renderer)
        self.interactor = self.vtk_widget.GetRenderWindow().GetInteractor()
        self.interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())
        self.picker = vtkPropPicker()
        self.interactor.AddObserver('RightButtonPressEvent', self.pick_at_cursor, 1.0)

        axes = vtkAxesActor()
        axes.SetTotalLength(1, 1, 1)
        self.axes_widget = vtkOrientationMarkerWidget()
        self.axes_widget.SetOrientationMarker(axes)
        self.axes_widget.SetInteractor(self.interactor)
        self.axes_widget.SetViewport(0, 0, 0.16, 0.23)
        self.axes_widget.SetEnabled(1)
        self.axes_widget.InteractiveOff()

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['Componentes / cuerpos (☑ visible)'])
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.itemChanged.connect(self.on_visibility_changed)
        self.tree.itemSelectionChanged.connect(self.highlight_selection)
        dock = QDockWidget('Árbol del modelo', self)
        dock.setObjectName('ArbolComponentes')
        dock.setWidget(self.tree)
        dock.setMinimumWidth(260)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        toolbar = QToolBar('Herramientas', self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        self.add_action(toolbar, 'Abrir STEP', self.open_dialog, 'Ctrl+O')
        self.add_action(toolbar, 'Mostrar todos', self.show_all)
        self.add_action(toolbar, 'Ocultar', self.hide_selected)
        self.add_action(toolbar, 'Aislar', self.isolate_selected)
        self.add_action(toolbar, 'Ajustar vista', self.fit_view, 'F')
        self.wire_action = QAction('Alámbrico', self)
        self.wire_action.setCheckable(True)
        self.wire_action.toggled.connect(self.set_wireframe)
        toolbar.addAction(self.wire_action)
        toolbar.addSeparator()
        toolbar.addWidget(QLabel(' Casillas: mostrar/ocultar  ·  Clic derecho 3D: seleccionar'))
        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage('Abre o arrastra aquí un archivo STEP/STP.')
        self.interactor.Initialize()
        self.render()

    def add_action(self, toolbar: QToolBar, title: str, callback: Any,
                   shortcut: str = '') -> None:
        action = QAction(title, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(lambda _checked=False, func=callback: func())
        toolbar.addAction(action)

    def render(self):
        self.vtk_widget.GetRenderWindow().Render()

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Abrir STEP/STP', '', 'Archivos STEP (*.step *.stp *.STEP *.STP)')
        if path:
            self.load_file(path)

    def load_file(self, path: str):
        self.statusBar().showMessage(f'Leyendo {Path(path).name}…')
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            nodes = read_step_model(path)
            new_actors = []
            new_items = []

            def create_item(node: ModelNode, parent: QTreeWidgetItem | None = None):
                item = QTreeWidgetItem([node.name])
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                item.setCheckState(0, Qt.Checked)
                if parent is None:
                    self.tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                if node.children:
                    for child in node.children:
                        create_item(child, item)
                elif node.poly is not None:
                    index = len(new_actors)
                    mapper = vtkPolyDataMapper()
                    mapper.SetInputData(node.poly)
                    mapper.ScalarVisibilityOff()
                    actor = vtkActor()
                    actor.SetMapper(mapper)
                    actor.GetProperty().SetColor(NORMAL_COLOR)
                    actor.GetProperty().SetSpecular(0.2)
                    actor.GetProperty().SetSpecularPower(25)
                    if self.wireframe:
                        actor.GetProperty().SetRepresentationToWireframe()
                    item.setData(0, Qt.UserRole, index)
                    new_actors.append(actor)
                    new_items.append(item)

            self.tree.blockSignals(True)
            try:
                for actor in self.actors:
                    self.renderer.RemoveActor(actor)
                self.tree.clear()
                for node in nodes:
                    create_item(node)
                self.tree.expandAll()
                self.actors = new_actors
                self.leaf_items = new_items
                for actor in self.actors:
                    self.renderer.AddActor(actor)
            finally:
                self.tree.blockSignals(False)
            self.setWindowTitle(f'{APP_NAME} · {Path(path).name}')
            self.fit_view()
            self.statusBar().showMessage(
                f'{Path(path).name} · {len(self.actors)} cuerpos. Selecciona, oculta o aísla desde el árbol.')
        except Exception as error:
            QMessageBox.critical(self, 'Error al abrir STEP', str(error))
            self.statusBar().showMessage('No se pudo abrir el archivo.')
        finally:
            QApplication.restoreOverrideCursor()

    @staticmethod
    def descendant_leaves(item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
        if item.childCount() == 0:
            return [item] if item.data(0, Qt.UserRole) is not None else []
        result = []
        for i in range(item.childCount()):
            result.extend(StepViewer.descendant_leaves(item.child(i)))
        return result

    def sync_group_states(self):
        def visit(item: QTreeWidgetItem):
            if item.childCount() == 0:
                return item.checkState(0)
            states = [visit(item.child(i)) for i in range(item.childCount())]
            if states and all(state == Qt.Checked for state in states):
                state = Qt.Checked
            elif states and all(state == Qt.Unchecked for state in states):
                state = Qt.Unchecked
            else:
                state = Qt.PartiallyChecked
            item.setCheckState(0, state)
            return state

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))

    def set_visible_indices(self, wanted: set[int], visible: bool):
        self.tree.blockSignals(True)
        try:
            for index in wanted:
                if 0 <= index < len(self.actors):
                    self.actors[index].SetVisibility(visible)
                    self.leaf_items[index].setCheckState(0, Qt.Checked if visible else Qt.Unchecked)
            self.sync_group_states()
        finally:
            self.tree.blockSignals(False)
        self.renderer.ResetCameraClippingRange()
        self.render()

    def on_visibility_changed(self, item: QTreeWidgetItem, _column: int):
        if item.checkState(0) == Qt.PartiallyChecked:
            return
        wanted = {leaf.data(0, Qt.UserRole) for leaf in self.descendant_leaves(item)}
        self.set_visible_indices(wanted, item.checkState(0) == Qt.Checked)

    def selected_leaf_indices(self) -> set[int]:
        selected = self.tree.selectedItems()
        if not selected:
            return set()
        return {leaf.data(0, Qt.UserRole)
                for leaf in self.descendant_leaves(selected[0])}

    def highlight_selection(self):
        selected = self.selected_leaf_indices()
        for i, actor in enumerate(self.actors):
            actor.GetProperty().SetColor(SELECTED_COLOR if i in selected else NORMAL_COLOR)
        self.render()

    def pick_at_cursor(self, _obj, _event):
        x, y = self.interactor.GetEventPosition()
        self.picker.Pick(x, y, 0, self.renderer)
        hit = self.picker.GetActor()
        for index, actor in enumerate(self.actors):
            if hit == actor:
                item = self.leaf_items[index]
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
                self.statusBar().showMessage(f'Seleccionado: {item.text(0)}')
                return

    def hide_selected(self):
        selected = self.selected_leaf_indices()
        if selected:
            self.set_visible_indices(selected, False)

    def isolate_selected(self):
        selected = self.selected_leaf_indices()
        if not selected:
            self.statusBar().showMessage('Selecciona un cuerpo o componente para aislarlo.')
            return
        self.set_visible_indices(set(range(len(self.actors))) - selected, False)
        self.set_visible_indices(selected, True)
        self.fit_view(reorient=False)

    def show_all(self):
        self.set_visible_indices(set(range(len(self.actors))), True)

    def fit_view(self, reorient: bool = True):
        if not self.actors or not any(actor.GetVisibility() for actor in self.actors):
            return
        self.renderer.ResetCamera()
        camera = self.renderer.GetActiveCamera()
        if reorient:
            camera.Azimuth(25)
            camera.Elevation(18)
        self.renderer.ResetCameraClippingRange()
        self.render()

    def set_wireframe(self, enabled: bool):
        self.wireframe = enabled
        for actor in self.actors:
            if enabled:
                actor.GetProperty().SetRepresentationToWireframe()
            else:
                actor.GetProperty().SetRepresentationToSurface()
        self.render()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(
            url.isLocalFile() and url.toLocalFile().lower().endswith(('.stp', '.step'))
            for url in event.mimeData().urls()
        ):
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(('.stp', '.step')):
                self.load_file(path)
                event.acceptProposedAction()
                break


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    viewer = StepViewer()
    viewer.show()
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        viewer.load_file(sys.argv[1])
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
