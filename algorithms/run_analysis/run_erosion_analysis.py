# -*- coding: utf-8 -*-

"""
/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

__author__ = "Ian Todd"
__date__ = "2022-02-09"
__copyright__ = "(C) 2022 by NOAA"

# This will get replaced with a git SHA1 when you do a git archive

__revision__ = "$Format:%H$"

from pathlib import Path
import sys
import math
import datetime
import json

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

from QNSPECT.qnspect_algorithm import QNSPECTAlgorithm


class RunErosionAnalysis(QNSPECTAlgorithm):
    lookupTable = "LookupTable"
    landUseType = "LandUseType"
    soilRaster = "SoilsRasterNotKfactor"
    kFactorRaster = "SoilsRaster"
    elevationRaster = "ElevationRaster"
    rFactorRaster = "RFactorRaster"
    landUseRaster = "LandUseRaster"
    lengthSlopeRaster = "LengthSlopeRaster"
    projectLocation = "ProjectLocation"
    mfd = 'MFD'
    rusle = "RUSLE"
    sedimentDeliveryRatio = "SedimentDeliveryRatio"
    sedimentYieldLocal = "Sediment Local"
    sedimentYieldAccumulated = "Sediment Accumulated"
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
                self.elevationRaster,
                "Elevation Raster",
                defaultValue=None,
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.rFactorRaster,
                "R-Factor Raster",
                defaultValue=None,
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.soilRaster, "Hydrographic Soils Group Raster", defaultValue=None
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
        param = QgsProcessingParameterBoolean(
            self.mfd, "Use Multi Flow Direction [MFD] Routing", defaultValue=False
        )
        param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(param)
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

        dem = self.parameterAsRasterLayer(parameters, self.elevationRaster, context)
        land_use_raster = self.parameterAsRasterLayer(
            parameters, self.landUseRaster, context
        )

        cell_size_sq_meters = self.cell_size_in_sq_meters(dem)
        if cell_size_sq_meters is None:
            raise QgsProcessingException("Invalid Elevation Raster CRS units.")

        lookup_layer = extract_lookup_table(
            self.parameterAsVectorLayer,
            self.parameterAsEnum, 
            parameters,
            context
        )
        if lookup_layer is None:
            raise QgsProcessingException(
                "Land Use Lookup Table must be provided with Custom Land Use Type."
            )
        # Folder I/O
        project_loc = Path(
            self.parameterAsString(parameters, self.projectLocation, context)
        )
        run_name = self.parameterAsString(
            parameters, self.runName, context
        )
        run_out_dir: Path = project_loc / run_name
        run_out_dir.mkdir(parents=True, exist_ok=True)

        ## RUSLE calculation section
        # K-factor - soil erodability
        erodability_raster = self.fill_zero_k_factor_cells(
            parameters, outputs, feedback, context
        )

        # C-factor - land cover
        c_factor_raster = self.create_c_factor_raster(
            lookup_layer=lookup_layer,
            land_use_raster_layer=land_use_raster,
            context=context,
            feedback=feedback,
            outputs=outputs,
        )

        # Length-slope factor
        ls_factor = self.create_ls_factor(parameters, context)

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

        ## Sediment Delivery Ratio calculation section
        # Relief length ratio part
        rl_raster = create_relief_length_ratio_raster(
            dem_raster=dem,
            cell_size_sq_meters=cell_size_sq_meters,
            output=QgsProcessing.TEMPORARY_OUTPUT,
            context=context,
            feedback=feedback,
            outputs=outputs,
        )

        # Curve number part
        cn = Curve_Number(
            parameters[self.landUseRaster],
            parameters[self.soilRaster],
            dual_soil_type=self.parameterAsEnum(parameters, self.dualSoils, context),
            lookup_layer=lookup_layer,
            context=context,
            feedback=feedback,
        )
        cn.generate_cn_raster()

        # Multiply RL and CN
        sdr = self.run_sediment_delivery_ratio(
            cell_size_sq_meters=cell_size_sq_meters,
            relief_length=rl_raster,
            curve_number=cn.cn_raster,
            context=context,
            feedback=feedback,
            outputs=outputs,
        )

        ## Component outputs section
        sediment_local = str(run_out_dir / (self.sedimentYieldLocal + ".tif"))
        self.run_sediment_yield(
            sediment_delivery_ratio=sdr,
            rusle=rusle,
            context=context,
            feedback=feedback,
            outputs=outputs,
            results=results,
            output=sediment_local,
        )
        if load_outputs:
            self.handle_post_processing(sediment_local, "Sediment Local (kg)", context)

        sediment_acc = str(run_out_dir / (self.sedimentYieldAccumulated + ".tif"))
        acc_results = self.run_sediment_yield_accumulated(
            sediment_yield=sediment_local,
            dem=dem,
            mfd=self.parameterAsBool(parameters, self.mfd, context),
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
            run_out_dir=run_out_dir,
            lookup_layer=lookup_layer,
            dem=dem,
            land_use_raster=land_use_raster,
            run_name=run_name,
        )

        return results

    def name(self):
        return "run_erosion_analysis"

    def displayName(self):
        return self.tr("Run Erosion Analysis")

    def group(self):
        return self.tr("Analysis")

    def groupId(self):
        return "analysis"

    def createInstance(self):
        return RunErosionAnalysis()

    def fill_zero_k_factor_cells(self, parameters, outputs, feedback, context):
        """Zero values in the K-Factor grid should be assumed "urban" and given a default value."""
        input_dict = {"input_a": parameters[self.kFactorRaster], "band_a": 1}
        expr = "((A == 0) * 0.3) + ((A > 0) * A)"
        outputs["KFill"] = perform_raster_math(
            exprs=expr,
            input_dict=input_dict,
            context=context,
            feedback=feedback,
        )
        return outputs["KFill"]["OUTPUT"]

    def create_c_factor_raster(
        self, lookup_layer, land_use_raster_layer, context, feedback, outputs
    ):
        # The c-factor raster will have floating-point values.
        # If the land use raster used is an integer type,
        # the assignment process will convert the c-factor values to integers.
        # Converting the land use raster to floating point type fixes that.
        land_use_raster = convert_raster_data_type_to_float(
            raster_layer=land_use_raster_layer,
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

    def cell_size_in_sq_meters(self, dem):
        """Converts the cell size of the DEM into meters.
        Returns None if the input raster's CRS is not usable."""
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
        exprs = "A * B * 907.18474"  # Multiply by 907.18474 to convert from ton to kg
        outputs[self.sedimentYieldLocal] = perform_raster_math(
            exprs=exprs,
            input_dict=input_dict,
            context=context,
            feedback=feedback,
            output=output,
        )
        results[self.sedimentYieldLocal] = outputs[self.sedimentYieldLocal]["OUTPUT"]

    def run_sediment_yield_accumulated(
        self,
        sediment_yield,
        dem,
        mfd,
        context,
        feedback,
        outputs,
        results,
        output,
    ):
        gmt = outputs[self.sedimentYieldAccumulated] = grass_material_transport(
            elevation=dem,
            weight=sediment_yield,
            context=context,
            feedback=feedback,
            output=output,
            mfd=mfd,
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
        self,
        parameters,
        context,
        results,
        run_out_dir: Path,
        lookup_layer,
        dem,
        land_use_raster,
        run_name,
    ):
        """Create a config file with the name of the run in the outputs folder. Uses the "ero" key word to differentiate it from the results of the pollution analysis."""
        config = {}
        config["Inputs"] = parameters
        config["Inputs"][self.elevationRaster] = dem.source()
        config["Inputs"][self.landUseRaster] = land_use_raster.source()
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
        config_file = run_out_dir / f"{run_name}.ero.json"
        json.dump(config, config_file.open("w"), indent=4)

    def handle_post_processing(self, layer, display_name, context):
        layer_details = context.LayerDetails(
            display_name, context.project(), display_name
        )
        # layer_details.setPostProcessor(self.grouper)
        context.addLayerToLoadOnCompletion(
            layer,
            layer_details,
        )

    def create_ls_factor(self, parameters, context):
        alg_params = {
            "-4": False,
            "-a": True,
            "-b": False,
            "-m": False,
            "-s": True,
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
        return processing.run(
            "grass7:r.watershed",
            alg_params,
            context=context,
            feedback=None,
            is_child_algorithm=True,
        )["length_slope"]
