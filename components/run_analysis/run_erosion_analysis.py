from pathlib import Path
import sys
import math
import datetime
import json

sys.path.append(str(Path(__file__).parent.parent.parent))
sys.path.append(str(Path(__file__).parent.parent))
sys.path.append(str(Path(__file__).parent))

from qnspect_utils import perform_raster_math, grass_material_transport
from analysis_utils import (
    extract_lookup_table,
    reclassify_land_use_raster_by_table_field,
    convert_raster_data_type_to_float,
    LAND_USE_TABLES,
)
from Curve_Number import Curve_Number
from relief_length_ratio import create_relief_length_ratio_raster

DEFAULT_URBAN_K_FACTOR_VALUE = 0.3

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingMultiStepFeedback,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterDefinition,
    QgsUnitTypes,
    QgsProcessingParameterString,
    QgsProcessingException,
)
import processing


class RunErosionAnalysis(QgsProcessingAlgorithm):
    lookupTable = "LookupTable"
    landUseType = "LandUseType"
    soilRaster = "SoilsRasterNotKfactor"
    kFactorRaster = "SoilsRaster"
    elevationRaster = "ElevationRaster"
    rFactorRaster = "RFactorRaster"
    landUseRaster = "LandUseRaster"
    lengthSlopeRaster = "LengthSlopeRaster"
    projectLocation = "ProjectLocation"
    mdf = "MDF"
    rusle = "RUSLE"
    sedimentDeliveryRatio = "SedimentDeliveryRatio"
    sedimentYieldLocal = "SedimentLocal"
    sedimentYieldAccumulated = "SedimentAccumulated"
    runName = "RunName"
    dualSoils = "DualSoils"
    loadOutputs = "LoadOutputs"

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterString(
                self.runName,
                "Run Name",
                multiLine=False,
                optional=False,
                defaultValue="",
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.elevationRaster, "Elevation Raster", defaultValue=None,
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.rFactorRaster, "R-Factor Raster", defaultValue=None,
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.soilRaster, "Soil Raster", defaultValue=None
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.kFactorRaster, "K-factor Raster", defaultValue=None
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.landUseRaster, "Land Use Raster", defaultValue=None
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.landUseType,
                "Land Use Type",
                options=["Custom"] + list(LAND_USE_TABLES.values()),
                allowMultiple=False,
                defaultValue=None,
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.lookupTable,
                "Land Use Lookup Table",
                optional=True,
                types=[QgsProcessing.TypeVector],
                defaultValue=None,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.loadOutputs,
                "Open output files after running algorithm",
                defaultValue=True,
            )
        )
        param = QgsProcessingParameterEnum(
            self.dualSoils,
            "Treat Dual Category Soils as",
            optional=False,
            options=["Undrained [Default]", "Drained", "Average"],
            allowMultiple=False,
            defaultValue=[0],
        )
        param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        param = QgsProcessingParameterBoolean(
            self.mdf, "Use Multi Direction Flow [MDF] Routing", defaultValue=False
        )
        param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.projectLocation,
                "Folder for Run Outputs",
                createByDefault=True,
                defaultValue=None,
            )
        )

    def processAlgorithm(self, parameters, context, model_feedback):
        # Use a multi-step feedback, so that individual child algorithm progress reports are adjusted for the
        # overall progress through the model
        feedback = QgsProcessingMultiStepFeedback(0, model_feedback)
        results = {}
        outputs = {}

        load_outputs: bool = self.parameterAsBool(parameters, self.loadOutputs, context)

        cell_size_sq_meters = self.cell_size_in_sq_meters(parameters, context)
        if cell_size_sq_meters is None:
            raise QgsProcessingException("Invalid Elevation Raster CRS units.")

        lookup_layer = extract_lookup_table(self, parameters, context)
        if lookup_layer is None:
            raise QgsProcessingException(
                "Land Use Lookup Table must be provided with Custom Land Use Type."
            )

        # Folder I/O
        project_loc = Path(
            self.parameterAsString(parameters, self.projectLocation, context)
        )
        run_out_dir: Path = project_loc / self.parameterAsString(
            parameters, self.runName, context
        )
        run_out_dir.mkdir(parents=True, exist_ok=True)

        # K-factor - soil erodability
        erodability_raster = self.fill_zero_k_factor_cells(
            parameters, outputs, feedback, context
        )

        # C-factor - land cover
        c_factor_raster = self.create_c_factor_raster(
            lookup_layer=lookup_layer,
            parameters=parameters,
            context=context,
            feedback=feedback,
            outputs=outputs,
        )

        ls_factor = self.create_ls_factor(parameters, context, outputs)

        rusle = self.run_rusle(
            c_factor=c_factor_raster,
            ls_factor=ls_factor,
            erodability=erodability_raster,
            cell_size_sq_meters=cell_size_sq_meters,
            parameters=parameters,
            context=context,
            feedback=feedback,
            outputs=outputs,
        )

        ## Sediment Delivery Ratio
        rl_raster = create_relief_length_ratio_raster(
            dem_raster=self.parameterAsRasterLayer(
                parameters, self.elevationRaster, context
            ),
            cell_size_sq_meters=cell_size_sq_meters,
            output=QgsProcessing.TEMPORARY_OUTPUT,
            context=context,
            feedback=feedback,
            outputs=outputs,
        )

        cn = Curve_Number(
            parameters[self.landUseRaster],
            parameters[self.soilRaster],
            dual_soil_type=self.parameterAsEnum(parameters, self.dualSoils, context),
            lookup_layer=extract_lookup_table(self, parameters, context),
            context=context,
            feedback=feedback,
        )
        cn.generate_cn_raster()

        sdr = self.run_sediment_delivery_ratio(
            cell_size_sq_meters=cell_size_sq_meters,
            relief_length=rl_raster,
            curve_number=cn.cn_raster,
            parameters=parameters,
            context=context,
            feedback=feedback,
            outputs=outputs,
        )

        sediment_local = str(run_out_dir / (self.sedimentYieldLocal + ".tif"))
        self.run_sediment_yield(
            sediment_delivery_ratio=sdr,
            rusle=rusle,
            context=context,
            feedback=feedback,
            parameters=parameters,
            outputs=outputs,
            results=results,
            output=sediment_local,
        )
        if load_outputs:
            self.handle_post_processing(
                sediment_local, "Local Accumulation (kg)", context
            )

        sediment_acc = str(run_out_dir / (self.sedimentYieldAccumulated + ".tif"))
        acc_results = self.run_sediment_yield_accumulated(
            sediment_yield=sediment_local,
            dem=parameters[self.elevationRaster],
            mdf=self.parameterAsBool(parameters, self.mdf, context),
            context=context,
            feedback=feedback,
            outputs=outputs,
            results=results,
            output=sediment_acc,
        )
        if load_outputs:
            self.handle_post_processing(
                acc_results, "Sediment Accumulation (Mg)", context
            )

        self.create_config_file(
            parameters=parameters,
            context=context,
            results=results,
            project_loc=project_loc,
        )

        return results

    def name(self):
        return "Run Erosion Analysis"

    def displayName(self):
        return "Run Erosion Analysis"

    def group(self):
        return "QNSPECT"

    def groupId(self):
        return "QNSPECT"

    def createInstance(self):
        return RunErosionAnalysis()

    def fill_zero_k_factor_cells(self, parameters, outputs, feedback, context):
        """Zero values in the K-Factor grid should be assumed "urban" and given a default value."""
        input_dict = {"input_a": parameters[self.kFactorRaster], "band_a": 1}
        expr = "((A == 0) * 0.3) + ((A > 0) * A)"
        outputs["KFill"] = perform_raster_math(
            exprs=expr, input_dict=input_dict, context=context, feedback=feedback,
        )
        return outputs["KFill"]["OUTPUT"]

    def create_c_factor_raster(
        self, lookup_layer, parameters, context, feedback, outputs
    ):
        # The c-factor raster will have floating-point values.
        # If the land use raster used is an integer type,
        # the assignment process will convert the c-factor values to integers.
        # Converting the land use raster to floating point type fixes that.
        land_use_raster = convert_raster_data_type_to_float(
            raster_layer=self.parameterAsRasterLayer(
                parameters, self.landUseRaster, context
            ),
            context=context,
            feedback=feedback,
            outputs=outputs,
            output=QgsProcessing.TEMPORARY_OUTPUT,
        )
        c_factor_raster = reclassify_land_use_raster_by_table_field(
            lu_raster=land_use_raster,
            lookup_layer=lookup_layer,
            value_field="c_factor",
            context=context,
            feedback=feedback,
            output=QgsProcessing.TEMPORARY_OUTPUT,
        )["OUTPUT"]
        return c_factor_raster

    def cell_size_in_sq_meters(self, parameters, context):
        """Converts the cell size of the DEM into meters.
        Returns None if the input raster's CRS is not usable."""
        dem = self.parameterAsRasterLayer(parameters, self.elevationRaster, context)
        size_x = dem.rasterUnitsPerPixelX()
        size_y = dem.rasterUnitsPerPixelY()
        area = size_x * size_y
        # Convert size into square kilometers
        raster_units = dem.crs().mapUnits()
        if raster_units == QgsUnitTypes.AreaSquareMeters:
            return area
        elif raster_units == QgsUnitTypes.AreaSquareKilometers:
            return area * 1_000_000.0
        elif raster_units == QgsUnitTypes.AreaSquareMiles:
            return area * 2_589_988.0
        elif raster_units == QgsUnitTypes.AreaSquareFeet:
            return area * 0.09290304

    def run_sediment_delivery_ratio(
        self,
        cell_size_sq_meters: float,
        relief_length,
        curve_number,
        parameters,
        context,
        feedback,
        outputs,
    ):
        """Runs a raster calculator using QGIS's native raster calculator class.
        GDAL does not allow float^float operations, so 'perform_raster_math' cannot be used here."""
        expr = " * ".join(
            [
                "1.366",
                "(10 ^ -11)",
                f"({(math.sqrt(cell_size_sq_meters) / 1_000.0) ** 2} ^ -0.0998)",  # convert to sq km
                f'("{Path(relief_length).stem}@1" ^ 0.3629)',
                f'("{Path(curve_number).stem}@1" ^ 5.444)',
            ]
        )
        alg_params = {
            "EXPRESSION": expr,
            "LAYERS": [relief_length, curve_number],
            "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
        }
        output = outputs[self.sedimentDeliveryRatio] = processing.run(
            "qgis:rastercalculator",
            alg_params,
            context=context,
            feedback=feedback,
            is_child_algorithm=True,
        )
        return output["OUTPUT"]

    def run_sediment_yield(
        self,
        sediment_delivery_ratio,
        rusle,
        context,
        feedback,
        parameters,
        outputs,
        results,
        output,
    ):
        input_dict = {
            "input_a": sediment_delivery_ratio,
            "band_a": 1,
            "input_b": rusle,
            "band_b": 1,
        }
        exprs = "A * B * 907.18474"
        outputs[self.sedimentYieldLocal] = perform_raster_math(
            exprs=exprs,
            input_dict=input_dict,
            context=context,
            feedback=feedback,
            output=output,
        )
        results[self.sedimentYieldLocal] = outputs[self.sedimentYieldLocal]["OUTPUT"]

    def run_sediment_yield_accumulated(
        self, sediment_yield, dem, mdf, context, feedback, outputs, results, output,
    ):
        gmt = outputs[self.sedimentYieldAccumulated] = grass_material_transport(
            elevation=dem,
            weight=sediment_yield,
            context=context,
            feedback=feedback,
            output=output,
            mfd=mdf,
        )
        result = gmt["accumulation"]
        results[self.sedimentYieldAccumulated] = result
        return result

    def run_rusle(
        self,
        c_factor,
        ls_factor,
        erodability,
        cell_size_sq_meters,
        parameters,
        context,
        feedback,
        outputs,
    ):
        ## Unit conversion in this function:
        ## -- A * B * C * D yields tons / acre
        ## -- multiply by 0.0002 to convert from acres to meters
        cell_size_acres = cell_size_sq_meters * 0.000247104369
        raster_math_params = {
            "input_a": c_factor,
            "input_b": ls_factor,
            "input_c": erodability,  # k-factor
            "input_d": parameters[self.rFactorRaster],  # rainfall_raster,
            "band_a": 1,
            "band_b": 1,
            "band_c": 1,
            "band_d": 1,
        }
        outputs[self.rusle] = perform_raster_math(
            f"A * B * C * D * {cell_size_acres}",
            raster_math_params,
            context,
            feedback,
            output=QgsProcessing.TEMPORARY_OUTPUT,
        )
        return outputs[self.rusle]["OUTPUT"]

    def create_config_file(
        self, parameters, context, results, project_loc: Path,
    ):
        lookup_layer = extract_lookup_table(self, parameters, context)
        config = {}
        config["Inputs"] = parameters
        config["Inputs"][self.elevationRaster] = self.parameterAsRasterLayer(
            parameters, self.elevationRaster, context
        ).source()
        config["Inputs"][self.landUseRaster] = self.parameterAsRasterLayer(
            parameters, self.landUseRaster, context
        ).source()
        config["Inputs"][self.kFactorRaster] = self.parameterAsRasterLayer(
            parameters, self.kFactorRaster, context
        ).source()
        config["Inputs"][self.soilRaster] = self.parameterAsRasterLayer(
            parameters, self.soilRaster, context
        ).source()
        config["Inputs"][self.rFactorRaster] = self.parameterAsRasterLayer(
            parameters, self.rFactorRaster, context
        ).source()
        if parameters[self.lookupTable]:
            config["Inputs"][self.lookupTable] = lookup_layer.source()
        config["Outputs"] = results
        config["RunTime"] = str(datetime.datetime.now())
        run_name: str = self.parameterAsString(parameters, self.runName, context)
        config_file = project_loc / f"{run_name}.ero.json"
        json.dump(config, config_file.open("w"), indent=4)

    def handle_post_processing(self, layer, display_name, context):
        layer_details = context.LayerDetails(
            display_name, context.project(), display_name
        )
        # layer_details.setPostProcessor(self.grouper)
        context.addLayerToLoadOnCompletion(
            layer, layer_details,
        )

    def create_ls_factor(self, parameters, context, outputs):
        alg_params = {
            "-4": False,
            "-a": True,
            "-b": False,
            "-m": False,
            "-s": not self.parameterAsBool(parameters, self.mdf, context),
            "GRASS_RASTER_FORMAT_META": "",
            "GRASS_RASTER_FORMAT_OPT": "",
            "GRASS_REGION_CELLSIZE_PARAMETER": 0,
            "GRASS_REGION_PARAMETER": None,
            "blocking": None,
            "convergence": 5,
            "depression": None,
            "disturbed_land": None,
            "elevation": parameters[self.elevationRaster],
            "flow": None,
            "max_slope_length": None,
            "memory": 300,
            "threshold": 500,
            "length_slope": QgsProcessing.TEMPORARY_OUTPUT,
        }
        outputs["RWatershed"] = processing.run(
            "grass7:r.watershed",
            alg_params,
            context=context,
            feedback=None,
            is_child_algorithm=True,
        )
        return outputs["RWatershed"]["length_slope"]
