"""STEP Ligero - Visor 3D de STEP/STP para Windows.
Dependencias: cadquery-ocp, vtk, PySide6.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QMainWindow, QMessageBox, QStatusBar,
    QToolBar, QLabel, QVBoxLayout, QWidget,
)
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
from vtkmodules.vtkCommonCore import vtkPoints
from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
from vtkmodules.vtkFiltersCore import vtkPolyDataNormals
from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper, vtkRenderer
from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera
from vtkmodules.vtkRenderingAnnotation import vtkAxesActor
from vtkmodules.vtkInteractionWidgets import vtkOrientationMarkerWidget
# Force load VTK's OpenGL backend and interaction implementations.
import vtkmodules.vtkRenderingOpenGL2  # noqa: F401
import vtkmodules.vtkRenderingFreeType  # noqa: F401
from OCP.STEPControl import STEPControl_Reader
from OCP.IFSelect import IFSelect_RetDone
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRep import BRep_Tool
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
from OCP.TopoDS import TopoDS
from OCP.TopLoc import TopLoc_Location

APP_NAME = 'STEP Ligero'


def read_step_mesh(path: str) -> tuple[vtkPolyData, int, int]:
    """Import STEP with OpenCascade; tessellate faces into a VTK mesh."""
    reader = STEPControl_Reader()
    result = reader.ReadFile(str(path))
    if result != IFSelect_RetDone:
        raise ValueError('No se pudo interpretar el archivo STEP.')
    nroots = reader.TransferRoots()
    if nroots < 1:
        raise ValueError('El archivo no contiene geometría transferible.')
    shape = reader.OneShape()
    if shape.IsNull():
        raise ValueError('No se encontró ningún sólido o superficie.')

    # Moderate tessellation quality. The values are in model units.
    BRepMesh_IncrementalMesh(shape, 0.25, False, 0.35, True)
    points = vtkPoints()
    triangles = vtkCellArray()
    faces = 0
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        loc = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, loc)
        if triangulation is not None:
            transform = loc.Transformation()
            offset = points.GetNumberOfPoints()
            for j in range(1, triangulation.NbNodes() + 1):
                p = triangulation.Node(j).Transformed(transform)
                points.InsertNextPoint(p.X(), p.Y(), p.Z())
            flip = face.Orientation() == TopAbs_REVERSED
            for j in range(1, triangulation.NbTriangles() + 1):
                a, b, c = triangulation.Triangle(j).Get()
                if flip:
                    b, c = c, b
                triangles.InsertNextCell(3)
                for k in (a, b, c):
                    triangles.InsertCellPoint(offset + k - 1)
            faces += 1
        explorer.Next()
    if not triangles.GetNumberOfCells():
        raise ValueError('El modelo no generó triángulos visibles.')

    mesh = vtkPolyData()
    mesh.SetPoints(points)
    mesh.SetPolys(triangles)
    normals = vtkPolyDataNormals()
    normals.SetInputData(mesh)
    normals.SetFeatureAngle(55.0)
    normals.SplittingOff()
    normals.ConsistencyOn()
    normals.AutoOrientNormalsOff()
    normals.Update()
    final = vtkPolyData()
    final.ShallowCopy(normals.GetOutput())
    return final, faces, triangles.GetNumberOfCells()


class StepViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME + ' · Visor STEP')
        self.resize(1100, 760)
        self.setMinimumSize(640, 460)
        self.setAcceptDrops(True)
        self.actor = None
        self.last_path = None
        self.wire = False

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        self.vtk_widget = QVTKRenderWindowInteractor(body)
        layout.addWidget(self.vtk_widget)
        self.setCentralWidget(body)
        self.renderer = vtkRenderer()
        self.renderer.SetBackground(0.075, 0.094, 0.13)
        self.renderer.SetBackground2(0.15, 0.20, 0.27)
        self.renderer.GradientBackgroundOn()
        self.vtk_widget.GetRenderWindow().AddRenderer(self.renderer)
        self.interactor = self.vtk_widget.GetRenderWindow().GetInteractor()
        self.interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())

        axes = vtkAxesActor()
        axes.SetTotalLength(1, 1, 1)
        self.axes_widget = vtkOrientationMarkerWidget()
        self.axes_widget.SetOrientationMarker(axes)
        self.axes_widget.SetInteractor(self.interactor)
        self.axes_widget.SetViewport(0.0, 0.0, 0.16, 0.22)
        self.axes_widget.SetEnabled(1)
        self.axes_widget.InteractiveOff()

        toolbar = QToolBar('Herramientas')
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        open_action = QAction('📂  Abrir STEP', self)
        open_action.setShortcut('Ctrl+O')
        open_action.triggered.connect(self.open_dialog)
        toolbar.addAction(open_action)
        reset_action = QAction('⌖  Ajustar vista', self)
        reset_action.setShortcut('F')
        reset_action.triggered.connect(self.fit_view)
        toolbar.addAction(reset_action)
        self.wire_action = QAction('▦  Alámbrico', self)
        self.wire_action.setCheckable(True)
        self.wire_action.toggled.connect(self.set_wireframe)
        toolbar.addAction(self.wire_action)
        toolbar.addSeparator()
        info = QLabel('  Arrastra un .step/.stp aquí · Izquierdo: rotar · Rueda: zoom · Central: desplazar')
        toolbar.addWidget(info)

        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage('Listo. Abre un archivo STEP para empezar.')
        self.interactor.Initialize()
        self.vtk_widget.GetRenderWindow().Render()

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Abrir modelo STEP', '', 'STEP (*.step *.stp *.STEP *.STP);;Todos (*.*)')
        if path:
            self.load_file(path)

    def load_file(self, path):
        self.statusBar().showMessage('Abriendo ' + Path(path).name + '…')
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            poly, faces, triangles = read_step_mesh(path)
            mapper = vtkPolyDataMapper()
            mapper.SetInputData(poly)
            mapper.ScalarVisibilityOff()
            actor = vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(0.53, 0.77, 0.96)
            actor.GetProperty().SetSpecular(0.25)
            actor.GetProperty().SetSpecularPower(24)
            actor.GetProperty().SetAmbient(0.12)
            if self.wire:
                actor.GetProperty().SetRepresentationToWireframe()
            if self.actor is not None:
                self.renderer.RemoveActor(self.actor)
            self.actor = actor
            self.renderer.AddActor(actor)
            self.last_path = path
            self.setWindowTitle(f'{APP_NAME} · {Path(path).name}')
            self.fit_view()
            self.statusBar().showMessage(f'{Path(path).name}  ·  {faces:,} caras  ·  {triangles:,} triángulos')
        except Exception as exc:
            QMessageBox.critical(self, 'No se pudo abrir el STEP', str(exc))
            self.statusBar().showMessage('Error al abrir el archivo.')
        finally:
            QApplication.restoreOverrideCursor()

    def fit_view(self):
        if self.actor is None:
            return
        self.renderer.ResetCamera()
        self.renderer.GetActiveCamera().Azimuth(25)
        self.renderer.GetActiveCamera().Elevation(18)
        self.renderer.ResetCameraClippingRange()
        self.vtk_widget.GetRenderWindow().Render()

    def set_wireframe(self, enabled):
        self.wire = enabled
        if self.actor is not None:
            prop = self.actor.GetProperty()
            if enabled:
                prop.SetRepresentationToWireframe()
            else:
                prop.SetRepresentationToSurface()
            self.vtk_widget.GetRenderWindow().Render()

    def dragEnterEvent(self, event: QDragEnterEvent):
        if any(u.isLocalFile() and u.toLocalFile().lower().endswith(('.stp', '.step')) for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(('.stp', '.step')):
                self.load_file(path)
                event.acceptProposedAction()
                break


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = StepViewer()
    window.show()
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        window.load_file(sys.argv[1])
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
