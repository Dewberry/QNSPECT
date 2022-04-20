# -*- coding: utf-8 -*-
"""
/***************************************************************************
 QNSPECT
                                 A QGIS plugin
 QGIS Plugin for NOAA Nonpoint Source Pollution and Erosion Comparison Tool (NSPECT)
 Generated by Plugin Builder: http://g-sherman.github.io/Qgis-Plugin-Builder/
                              -------------------
        begin                : 2021-12-29
        copyright            : (C) 2021 by NOAA
        email                : ocm dot nspect dot admins at noaa dot gov
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
 This script initializes the plugin, making it known to QGIS.
"""

__author__ = "Abdul Raheem Siddiqui"
__date__ = "2021-12-29"
__copyright__ = "(C) 2021 by NOAA"


# noinspection PyPep8Naming
def classFactory(iface):  # pylint: disable=invalid-name
    """Load QNSPECT class from file QNSPECT.

    :param iface: A QGIS interface instance.
    :type iface: QgsInterface
    """
    #
    from .qnspect import QNSPECTPlugin

    return QNSPECTPlugin(iface)
