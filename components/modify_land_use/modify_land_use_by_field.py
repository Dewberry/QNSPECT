from qgis.core import QgsProcessing
from qgis.core import QgsProcessingAlgorithm
from qgis.core import QgsProcessingMultiStepFeedback
from qgis.core import QgsProcessingParameterVectorLayer
from qgis.core import QgsProcessingParameterRasterLayer
from qgis.core import QgsProcessingParameterField
from qgis.core import QgsProcessingParameterRasterDestination
from qgis.core import QgsVectorLayer
import processing


class ModifyLandUse(QgsProcessingAlgorithm):
    inputVector = "InputVector"
    field = "Field"
    inputRaster = "InputRaster"
    output = "OutputRaster"

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.inputVector, "Areas to Modify", defaultValue=None
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.field,
                "Land Use Value Field",
                optional=True,
                type=QgsProcessingParameterField.Numeric,
                parentLayerParameterName=self.inputVector,
                allowMultiple=False,
                defaultValue=None,
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.inputRaster, "Land Use Raster", defaultValue=None
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.output, "Modified Raster", createByDefault=True, defaultValue=None
            )
        )

    def processAlgorithm(self, parameters, context, model_feedback):
        # Use a multi-step feedback, so that individual child algorithm progress reports are adjusted for the
        # overall progress through the model
        # Rasterize with overwrite was the best method for accomplishing this. Since it directly changes the input, the original needs to be copied prior to execution
        feedback = QgsProcessingMultiStepFeedback(0, model_feedback)
        results = {}
        outputs = {}

        # Uses clip raster to get a copy of the original raster
        # Some other method of copying in a way that allows for temporary output would be better for this part
        alg_params = {
            "DATA_TYPE": 0,
            "EXTRA": "",
            "INPUT": parameters[self.inputRaster],
            "NODATA": None,
            "OPTIONS": "",
            "PROJWIN": parameters[self.inputRaster],
            "OUTPUT": parameters[self.output],
        }
        outputs["ClipRasterByExtent"] = processing.run(
            "gdal:cliprasterbyextent",
            alg_params,
            context=context,
            feedback=feedback,
            is_child_algorithm=True,
        )

        feedback.setCurrentStep(1)
        if feedback.isCanceled():
            return {}

        # Use only the selected features of the vector layer
        vector_layer = self.parameterAsVectorLayer(
            parameters, self.inputVector, context
        )
        if len(vector_layer.selectedFeatures()):
            selected_features = QgsVectorLayer(
                f"Polygon?crs={vector_layer.crs().toWkt()}",
                "selected_features",
                "memory",
            )
            data_provider = selected_features.dataProvider()
            data_provider.addFeatures(vector_layer.selectedFeatures())
            selected_features.commitChanges()
            selected_features.updateExtents()
            vector_layer = selected_features

        # Rasterize (overwrite with attribute)
        alg_params = {
            "ADD": False,
            "EXTRA": "",
            "FIELD": parameters[self.field],
            "INPUT": vector_layer,
            "INPUT_RASTER": outputs["ClipRasterByExtent"]["OUTPUT"],
        }
        outputs["RasterizeOverwriteWithAttribute"] = processing.run(
            "gdal:rasterize_over",
            alg_params,
            context=context,
            feedback=feedback,
            is_child_algorithm=True,
        )

        return results

    def name(self):
        return "Modify Land Use by Field"

    def displayName(self):
        return "Modify Land Use by Field"

    def group(self):
        return "QNSPECT"

    def groupId(self):
        return "QNSPECT"

    def createInstance(self):
        return ModifyLandUse()
