from __future__ import division, print_function, absolute_import

"""
Author      : Lyubimov, A.Y.
Created     : 10/10/2014
Last Changed: 10/30/2018
Description : Runs DIALS spotfinding, indexing, refinement and integration
              modules. The entire thing works, but no optimization of parameters
              is currently available. This is very much a work in progress
"""

import os
import sys

from iotbx.phil import parse
from dxtbx.datablock import DataBlockFactory
from cctbx import sgtbx
import copy

from dials.array_family import flex
from dials.command_line.stills_process import phil_scope, Processor
from dials.command_line.refine_bravais_settings import phil_scope as sg_scope
from dials.command_line.refine_bravais_settings import (
    bravais_lattice_to_space_group_table,
)

import iota.components.iota_utils as util


class IOTADialsProcessor(Processor):
    """Subclassing the Processor module from dials.stills_process to introduce
    streamlined integration pickles output."""

    def __init__(self, params, write_pickle=True):
        self.phil = params
        self.write_pickle = write_pickle
        Processor.__init__(self, params=params)

    def refine_bravais_settings(self, reflections, experiments):
        proc_scope = phil_scope.format(python_object=self.phil)
        sgparams = sg_scope.fetch(proc_scope).extract()
        sgparams.refinement.reflections.outlier.algorithm = "tukey"

        crystal_P1 = copy.deepcopy(experiments[0].crystal)

        from dials.algorithms.indexing.symmetry import (
            refined_settings_factory_from_refined_triclinic,
        )

        # Generate Bravais settings
        try:
            Lfat = refined_settings_factory_from_refined_triclinic(
                sgparams,
                experiments,
                reflections,
                lepage_max_delta=5,
                refiner_verbosity=10,
            )
        except Exception, e:
            # If refinement fails, reset to P1 (experiments remain modified by Lfat
            # if there's a refinement failure, which causes issues down the line)
            for expt in experiments:
                expt.crystal = crystal_P1
            return None

        Lfat.labelit_printout()

        # Filter out not-recommended (i.e. too-high rmsd and too-high max angular
        # difference) solutions
        Lfat_recommended = [s for s in Lfat if s.recommended]

        # If none are recommended, return None (do not reindex)
        if len(Lfat_recommended) == 0:
            return None

        # Find the highest symmetry group
        possible_bravais_settings = set(
            solution["bravais"] for solution in Lfat_recommended
        )
        bravais_lattice_to_space_group_table(possible_bravais_settings)
        lattice_to_sg_number = {
            "aP": 1,
            "mP": 3,
            "mC": 5,
            "oP": 16,
            "oC": 20,
            "oF": 22,
            "oI": 23,
            "tP": 75,
            "tI": 79,
            "hP": 143,
            "hR": 146,
            "cP": 195,
            "cF": 196,
            "cI": 197,
        }
        filtered_lattices = {}
        for key, value in lattice_to_sg_number.items():
            if key in possible_bravais_settings:
                filtered_lattices[key] = value

        highest_sym_lattice = max(filtered_lattices, key=filtered_lattices.get)
        highest_sym_solutions = [s for s in Lfat if s["bravais"] == highest_sym_lattice]
        if len(highest_sym_solutions) > 1:
            highest_sym_solution = sorted(
                highest_sym_solutions, key=lambda x: x["max_angular_difference"]
            )[0]
        else:
            highest_sym_solution = highest_sym_solutions[0]

        return highest_sym_solution

    def reindex(self, reflections, experiments, solution):
        """Reindex with newly-determined space group / unit cell."""

        # Update space group / unit cell
        experiment = experiments[0]
        print("Old crystal:")
        print(experiment.crystal, "\n")
        experiment.crystal.update(solution.refined_crystal)
        print("New crystal:")
        print(experiment.crystal, "\n")

        # Change basis
        cb_op = solution["cb_op_inp_best"].as_abc()
        change_of_basis_op = sgtbx.change_of_basis_op(cb_op)
        miller_indices = reflections["miller_index"]
        non_integral_indices = change_of_basis_op.apply_results_in_non_integral_indices(
            miller_indices
        )
        if non_integral_indices.size() > 0:
            print(
                "Removing {}/{} reflections (change of basis results in non-integral indices)"
                "".format(non_integral_indices.size(), miller_indices.size())
            )
        sel = flex.bool(miller_indices.size(), True)
        sel.set_selected(non_integral_indices, False)
        miller_indices_reindexed = change_of_basis_op.apply(miller_indices.select(sel))
        reflections["miller_index"].set_selected(sel, miller_indices_reindexed)
        reflections["miller_index"].set_selected(~sel, (0, 0, 0))

        return experiments, reflections

    def write_integration_pickles(self, integrated, experiments, callback=None):
        """This is streamlined vs.

        the code in stills_indexer, since the filename convention is set
        up upstream.
        """
        if self.write_pickle:
            from libtbx import easy_pickle
            from xfel.command_line.frame_extractor import ConstructFrame

            self.frame = ConstructFrame(integrated, experiments[0]).make_frame()
            self.frame["pixel_size"] = experiments[0].detector[0].get_pixel_size()[0]
            easy_pickle.dump(self.phil.output.integration_pickle, self.frame)


class Triage(object):
    """Performs quick spotfinding (with mostly defaults) and determines if the
    number of found reflections is above the minimum, and thus if the image
    should be accepted for further processing."""

    def __init__(self, img, gain, params, center_intensity=0):
        """Initialization and data read-in."""
        self.gain = gain
        self.params = params

        # Read settings from the DIALS target (.phil) file
        # If none is provided, use default settings (and may God have mercy)
        if self.params.cctbx_xfel.target is not None:
            with open(self.params.cctbx_xfel.target, "r") as settings_file:
                settings_file_contents = settings_file.read()
            settings = parse(settings_file_contents)
            current_phil = phil_scope.fetch(sources=[settings])
            self.phil = current_phil.extract()
        else:
            self.phil = phil_scope.extract()

        # Modify settings
        self.phil.output.strong_filename = None
        self.processor = IOTADialsProcessor(params=self.phil)

        # Set customized parameters
        beamX = self.params.image_import.beam_center.x
        beamY = self.params.image_import.beam_center.y
        if beamX != 0 or beamY != 0:
            self.phil.geometry.detector.slow_fast_beam_centre = "{} {}".format(
                beamY, beamX
            )
        if self.params.image_import.distance != 0:
            self.phil.geometry.detector.distance = self.params.image_import.distance
        if self.params.advanced.estimate_gain:
            self.phil.spotfinder.threshold.dispersion.gain = gain
        if self.params.image_import.mask is not None:
            self.phil.spotfinder.lookup.mask = self.params.image_import.mask
            self.phil.integration.lookup.mask = self.params.image_import.mask

        if self.params.cctbx_xfel.auto_threshold:
            threshold = int(center_intensity)
            self.phil.spotfinder.threshold.dispersion.global_threshold = threshold

        # Convert raw image into single-image datablock
        with util.Capturing() as junk_output:
            self.datablock = DataBlockFactory.from_filenames([img])[0]

    def triage_image(self):
        """Perform triage by running spotfinding and analyzing results."""

        # Triage image
        try:
            observed = self.processor.find_spots(datablock=self.datablock)
            if len(observed) >= self.params.cctbx_ha14.image_triage.min_Bragg_peaks:
                log_info = "ACCEPTED! {} observed reflections.".format(len(observed))
                status = None
            else:
                log_info = "REJECTED! {} observed reflections.".format(len(observed))
                status = "failed triage"
        except Exception, e:
            status = "failed triage"
            return status, "REJECTED! SPOT-FINDING ERROR!"

        return status, log_info


class Integrator(object):
    """A class for indexing, integration, etc.

    using DIALS modules
    """

    def __init__(
        self,
        source_image,
        object_folder,
        int_folder,
        final_filename,
        final,
        logfile,
        gain=0.32,
        center_intensity=0,
        params=None,
    ):
        """Initialise the script."""

        self.params = params
        self.int_base = int_folder

        # Read settings from the DIALS target (.phil) file
        # If none is provided, use default settings (and may God have mercy)
        if self.params.cctbx_xfel.target is not None:
            with open(self.params.cctbx_xfel.target, "r") as settings_file:
                settings_file_contents = settings_file.read()
            settings = parse(settings_file_contents)
            current_phil = phil_scope.fetch(source=settings)
        else:
            current_phil = phil_scope
        self.phil = current_phil.extract()

        # Set general file-handling settings
        file_basename = util.make_filename(source_image)
        self.phil.output.datablock_filename = "{}/{}.json".format(
            object_folder, file_basename
        )
        self.phil.output.indexed_filename = "{}/{}_indexed.pickle".format(
            object_folder, file_basename
        )
        self.phil.output.strong_filename = "{}/{}_strong.pickle".format(
            object_folder, file_basename
        )
        self.phil.output.refined_experiments_filename = "{}/{}_refined_experiments.json".format(
            object_folder, file_basename
        )
        self.phil.output.integrated_experiments_filename = "{}/{}_integrated_experiments.json".format(
            object_folder, file_basename
        )
        self.phil.output.integrated_filename = "{}/{}_integrated.pickle".format(
            object_folder, file_basename
        )
        self.phil.output.profile_filename = "{}/{}_profile.phil".format(
            object_folder, file_basename
        )
        self.phil.output.integration_pickle = final_filename
        self.int_log = logfile

        # Set customized parameters
        beamX = self.params.image_import.beam_center.x
        beamY = self.params.image_import.beam_center.y
        if beamX != 0 or beamY != 0:
            self.phil.geometry.detector.slow_fast_beam_centre = "{} {}".format(
                beamY, beamX
            )
        if self.params.image_import.distance != 0:
            self.phil.geometry.detector.distance = self.params.image_import.distance
        if self.params.image_import.mask is not None:
            self.phil.spotfinder.lookup.mask = self.params.image_import.mask
            self.phil.integration.lookup.mask = self.params.image_import.mask

        if self.params.cctbx_xfel.target_space_group is not None:
            sg = self.params.cctbx_xfel.target_space_group
            self.phil.indexing.known_symmetry.space_group = sg

        if self.params.cctbx_xfel.target_unit_cell is not None:
            uc = self.params.cctbx_xfel.target_unit_cell
            self.phil.indexing.known_symmetry.unit_cell = uc

        if self.params.cctbx_xfel.use_fft3d:
            self.phil.indexing.stills.method_list = [
                "fft1d",
                "fft3d",
                "real_space_grid_search",
            ]
        if self.params.cctbx_xfel.significance_filter.flag_on:
            if self.params.cctbx_xfel.significance_filter.sigma is not None:
                sigma = self.params.cctbx_xfel.significance_filter.sigma
                self.phil.significance_filter.enable = True
                self.phil.significance_filter.isigi_cutoff = sigma

        self.img = [source_image]
        self.obj_base = object_folder
        self.fail = None
        self.frame = None
        self.final = final
        self.final["final"] = final_filename
        self.datablock = DataBlockFactory.from_filenames(self.img)[0]
        self.obj_filename = "int_{}".format(os.path.basename(self.img[0]))

        # Auto-set threshold and gain (not saved for target.phil)
        if self.params.cctbx_xfel.auto_threshold:
            # This is still experimental and I'm not sure if it does anything...

            # rad_avg = RadAverageCalculator(datablock=self.datablock)
            # means, res = rad_avg.make_radial_average(num_bins=20, lowres=90)
            # threshold = int(np.min(means) * 5)
            threshold = int(center_intensity)
            self.phil.spotfinder.threshold.dispersion.global_threshold = threshold
        if self.params.advanced.estimate_gain:
            self.phil.spotfinder.threshold.dispersion.gain = gain

    def find_spots(self):
        # Perform spotfinding
        self.observed = self.processor.find_spots(datablock=self.datablock)

    def index(self):
        # Run indexing
        self.experiments, self.indexed = self.processor.index(
            datablock=self.datablock, reflections=self.observed
        )

    def refine_bravais_settings_and_reindex(self):
        # Find highest-symmetry Bravais lattice
        solution = self.processor.refine_bravais_settings(
            reflections=self.indexed, experiments=self.experiments
        )

        # Only reindex if higher-symmetry solution found
        if solution is not None:
            self.experiments, self.indexed = self.processor.reindex(
                reflections=self.indexed,
                experiments=self.experiments,
                solution=solution,
            )

    def refine(self):
        # Run refinement
        self.experiments, self.indexed = self.processor.refine(
            experiments=self.experiments, centroids=self.indexed
        )

    def integrate(self):
        # Run integration
        self.integrated = self.processor.integrate(
            experiments=self.experiments, indexed=self.indexed
        )
        self.frame = self.processor.frame

    def run(self):

        self.processor = IOTADialsProcessor(params=self.phil)

        log_entry = ["\n"]
        with util.Capturing() as output:
            e = None
            try:
                print("{:-^100}\n".format(" SPOTFINDING: "))
                self.find_spots()
                print(
                    "{:-^100}\n\n".format(
                        " FOUND {} SPOTS: ".format(len(self.observed))
                    )
                )
            except Exception as e:
                if hasattr(e, "classname"):
                    print(e.classname, "for %s:" % self.img[0])
                    error_message = "{}: {}".format(
                        e.classname, e[0].replace("\n", " ")[:50]
                    )
                else:
                    print("Spotfinding error for %s:" % self.img[0])
                    error_message = "{}".format(str(e).replace("\n", " ")[:50])
                print(error_message)
                self.fail = "failed spotfinding"

            if self.fail is None:
                try:
                    print("{:-^100}\n".format(" INDEXING: "))
                    self.index()
                    if self.indexed is not None:
                        print(
                            "{:-^100}\n\n".format(
                                " USED {} INDEXED REFLECTIONS: "
                                "".format(len(self.indexed))
                            )
                        )
                except Exception, e:
                    if hasattr(e, "classname"):
                        error_message = "{}: {}".format(
                            e.classname, e[0].replace("\n", " ")[:50]
                        )
                    else:
                        print("Indexing error for %s:" % self.img[0])
                        error_message = "{}".format(str(e).replace("\n", " ")[:50])
                    print(error_message)
                    self.fail = "failed indexing"

            if (
                self.fail is None
                and self.phil.indexing.known_symmetry.space_group is None
                and self.params.cctbx_xfel.determine_sg_and_reindex
            ):
                try:
                    print("{:-^100}\n".format(" DETERMINING SPACE GROUP : "))
                    self.refine_bravais_settings_and_reindex()
                    lat = self.experiments[0].crystal.get_space_group().info()
                    sg = str(lat).replace(" ", "")
                    if sg != "P1":
                        print(
                            "{:-^100}\n".format(
                                " REINDEXED TO SPACE GROUP {} ".format(sg)
                            )
                        )
                    else:
                        print("{:-^100}\n".format(" RETAINED TRICLINIC (P1) SYMMETRY "))
                except Exception as e:
                    print("Bravais / Reindexing Error: ", e)

            if self.fail is None:
                try:
                    self.refine()
                    print("{:-^100}\n".format(" INTEGRATING: "))
                    self.integrate()
                    print(
                        "{:-^100}\n\n".format(
                            " FINAL {} INTEGRATED REFLECTIONS "
                            "".format(len(self.integrated))
                        )
                    )
                except Exception as e:
                    if hasattr(e, "classname"):
                        print(e.classname, "for %s:" % self.img[0])
                        error_message = "{}: {}".format(
                            e.classname, e[0].replace("\n", " ")[:50]
                        )
                    else:
                        print("Integration error for %s:" % self.img[0])
                        error_message = "{}".format(str(e).replace("\n", " ")[:50])
                    print(error_message)
                    self.fail = "failed integration"

        if self.fail is None and self.params.cctbx_xfel.filter.flag_on:
            selector = Selector(
                frame=self.frame,
                uc_tol=self.params.cctbx_xfel.filter.target_uc_tolerance,
                pg=self.params.cctbx_xfel.filter.target_pointgroup,
                uc=self.params.cctbx_xfel.filter.target_unit_cell,
                min_ref=self.params.cctbx_xfel.filter.min_reflections,
                min_res=self.params.cctbx_xfel.filter.min_resolution,
            )
            self.fail = selector.result_filter()

        with open(self.int_log, "w") as tf:
            for i in output:
                if "cxi_version" not in i:
                    tf.write("\n{}".format(i))

        if self.fail is None:
            # Collect information
            obs = self.frame["observations"][0]
            Bravais_lattice = self.frame["pointgroup"]
            cell = obs.unit_cell().parameters()
            lres, hres = obs.d_max_min()

            # Calculate number of spots w/ high I / sigmaI
            Is = obs.data()
            sigmas = obs.sigmas()
            I_over_sigI = Is / sigmas
            spots = len(Is)
            strong_spots = len(
                [
                    i
                    for i in I_over_sigI
                    if i >= self.params.cctbx_ha14.selection.min_sigma
                ]
            )

            # Mosaicity parameters
            mosaicity = round((self.frame.get("ML_half_mosaicity_deg", [0])[0]), 6)
            dom_size = self.frame.get("ML_domain_size_ang", [0])[0]
            ewald_proximal_volume = self.frame.get("ewald_proximal_volume", [0])[0]

            # Assemble output for log file and/or integration result file
            p_cell = (
                "{:>6.2f}, {:>6.2f}, {:>6.2f}, {:>6.2f}, {:>6.2f}, {:>6.2f}"
                "".format(cell[0], cell[1], cell[2], cell[3], cell[4], cell[5])
            )

            int_status = "RES: {:<4.2f}  NSREF: {:<4}  SG: {:<5}  CELL: {}" "".format(
                hres, strong_spots, Bravais_lattice, p_cell
            )

            int_results = {
                "sg": Bravais_lattice,
                "a": cell[0],
                "b": cell[1],
                "c": cell[2],
                "alpha": cell[3],
                "beta": cell[4],
                "gamma": cell[5],
                "wavelength": self.frame["wavelength"],
                "distance": self.frame["distance"],
                "beamX": self.frame["xbeam"],
                "beamY": self.frame["ybeam"],
                "strong": strong_spots,
                "res": hres,
                "lres": lres,
                "mos": mosaicity,
                "epv": ewald_proximal_volume,
                "info": int_status,
                "ok": True,
            }

            # Update final entry with integration results
            self.final.update(int_results)

            # Generate log summary of integration results
            img_filename = os.path.basename(self.img[0])
            log_entry.append("DIALS integration:")
            log_entry.append(
                "{:<{width}} --->  {}".format(
                    img_filename, int_status, width=len(img_filename) + 2
                )
            )

        else:
            # Generate log summary of integration results
            if "spotfinding" in self.fail:
                step_id = "SPOTFINDING"
            elif "indexing" in self.fail:
                step_id = "INDEXING"
            elif "integration" in self.fail:
                step_id = "INTEGRATION"
            elif "filter" in self.fail:
                step_id = "FILTER"
            log_entry.append("\n {} FAILED - {}".format(step_id, e))
            int_status = "not integrated -- {}".format(e)
            int_results = {"info": int_status}
            self.final["final"] = None

        log_entry = "\n".join(log_entry)

        return self.fail, self.final, log_entry


class Selector(object):
    """Class for selection of optimal spotfinding parameters from grid
    search."""

    def __init__(self, frame, uc_tol=0, pg=None, uc=None, min_ref=0, min_res=None):

        obs = frame["observations"][0]
        self.obs_pg = frame["pointgroup"]
        self.obs_uc = [prm for prm in obs.unit_cell().parameters()]
        self.obs_res = obs.d_max_min()[1]
        self.obs_ref = len(obs.data())
        self.uc = uc
        self.uc_tol = uc_tol
        self.pg = pg
        self.min_ref = min_ref
        self.min_res = min_res
        self.fail = None

    def result_filter(self):
        """Unit cell pre-filter.

        Applies hard space-group constraint and stringent unit cell
        parameter restraints to filter out integration results that
        deviate. Optional step. Unit cell tolerance user-defined.
        """

        if self.uc is not None:
            user_uc = [prm for prm in self.uc.parameters()]
            delta_a = abs(self.obs_uc[0] - user_uc[0])
            delta_b = abs(self.obs_uc[1] - user_uc[1])
            delta_c = abs(self.obs_uc[2] - user_uc[2])
            delta_alpha = abs(self.obs_uc[3] - user_uc[3])
            delta_beta = abs(self.obs_uc[4] - user_uc[4])
            delta_gamma = abs(self.obs_uc[5] - user_uc[5])
            uc_check = (
                delta_a <= user_uc[0] * self.uc_tol
                and delta_b <= user_uc[1] * self.uc_tol
                and delta_c <= user_uc[2] * self.uc_tol
                and delta_alpha <= user_uc[3] * self.uc_tol
                and delta_beta <= user_uc[4] * self.uc_tol
                and delta_gamma <= user_uc[5] * self.uc_tol
            )
        else:
            uc_check = True

        i_fail = (
            self.obs_ref <= self.min_ref
            or (self.min_res is not None and self.obs_res >= self.min_res)
            or (
                self.pg is not None
                and self.pg.replace(" ", "") != self.obs_pg.replace(" ", "")
            )
            or not uc_check
        )

        if i_fail:
            fail = "failed filter"
        else:
            fail = None

        return fail


# ============================================================================ #

if __name__ == "__main__":

    # noinspection PyArgumentList
    test = Integrator(sys.argv[1])
    test.find_spots()

    print(len(test.observed))

    test.index()
    print(len(test.indexed))
