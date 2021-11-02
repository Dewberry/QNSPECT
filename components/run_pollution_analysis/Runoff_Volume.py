from qgis.core import (
    QgsProcessingMultiStepFeedback,
    QgsRasterLayer,
    QgsDistanceArea,
    QgsCoordinateTransformContext,
    QgsUnitTypes,
)
from .qnspect_utils import perform_raster_math


class Runoff_Volume:
    """Class to generate and store Runoff Volume Raster"""

    outputs = {}

    def __init__(
        self,
        precip_raster: str,
        cn_raster: str,
        ref_raster: QgsRasterLayer,
        precip_units: int,
        rainy_days: int,
        context,
        feedback: QgsProcessingMultiStepFeedback,
    ):
        self.precip_raster = precip_raster
        self.cn_raster = cn_raster
        self.ref_raster = ref_raster
        self.precip_units = precip_units
        self.rainy_days = rainy_days
        self.context = context
        self.feedback = feedback

    def preprocess_precipitation(self) -> None:
        if self.precip_units == 1:
            input_params = {
                "input_a": self.precip_raster,
                "band_a": "1",
            }
            self.outputs["P"] = perform_raster_math("A/25.4", input_params)
            self.precip_raster_in = self.outputs["P"]["OUTPUT"]
        else:
            self.precip_raster_in = self.precip_raster

    def calculate_S(self) -> None:
        """Calculate S (Potential Maximum Retention) (inches)"""
        input_params = {
            "input_a": self.cn_raster,
            "band_a": "1",
        }
        self.outputs["S"] = perform_raster_math(
            "(1000/A)-10",
            input_params,
        )

    def calculate_Q(self) -> dict:
        """Calculate runoff volume in Liters"""

        cell_area = (
            self.ref_raster.rasterUnitsPerPixelY()
            * self.ref_raster.rasterUnitsPerPixelX()
        )

        d = QgsDistanceArea()
        tr_cont = QgsCoordinateTransformContext()
        d.setSourceCrs(self.ref_raster.crs(), tr_cont)
        cell_area_sq_feet = d.convertAreaMeasurement(
            cell_area, QgsUnitTypes.AreaSquareFeet
        )

        input_params = {
            "input_a": self.precip_raster,
            "band_a": "1",
            "input_b": self.outputs["S"]["OUTPUT"],
            "band_b": "1",
        }

        # (Volume) (L)
        self.outputs["Q"] = perform_raster_math(
            # (((Precip-(0.2*S*rainy_days))**2)/(Precip+(0.8*S*rainy_days)) * [If (Precip-0.2S)<0, set to 0] * cell area to convert to vol * (28.3168/12) to convert inches to feet and cubic feet to Liters",
            f"(((A-(0.2*B*{self.rainy_days}))**2)/(A+(0.8*B*{self.rainy_days})) * ((A-(0.2*B*{self.rainy_days}))>0)) * {cell_area_sq_feet} * 2.35973722 ",
            input_params,
        )

        self.runoff_vol_raster = self.outputs["Q"]["OUTPUT"]

        return self.outputs["Q"]