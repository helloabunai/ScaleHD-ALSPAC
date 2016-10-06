from __future__ import division

#/usr/bin/python
__version__ = 0.01
__author__ = 'alastair.maxwell@glasgow.ac.uk'

##
## Generic imports
import sys
import os
import csv
import peakutils
import numpy as np
import logging as log
from collections import Counter
from sklearn import svm
from sklearn import preprocessing
from sklearn.multiclass import OneVsOneClassifier
import matplotlib
matplotlib.use('Agg') #servers/clients without x-11
from peakutils.plot import plot as pplot
import matplotlib.pyplot as plt
import pandas as pd

##
## Backend Junk
from ..__backend import Colour as clr
from ..__backend import DataLoader


class GenotypePrediction:
	def __init__(self, data_pair, prediction_path, training_data, instance_params):
		"""
		Prediction stage of the pipeline -- use of SVM, density estimation and first order differentials.
		Automates calling of sample's genotype based on data information dervied from aligned read counts.
		Utilises forward reads for CAG information, reverse reads for CCG information. Combine for genotype.

		General workflow overview:
		--Take reverse reads, aggregate every CAG for each CCG bin
		--Use unlabelled sample into CCG zygosity SVM for het/hom prediction
		--Data cleaning/normalisation/etc
		--Two Pass algorithm to determine genotype
		--1) Density Estimation on distribution to gauge where the peaks may be/peak distances
		--2) Peak Detection via first order differentials, taking into account density results for tailoring
		--Repeat process for relevant CAG distribution(s) taking CCG het/hom into account
		--Return genotype

		:param data_pair: Files to be used for scraping
		:param prediction_path: Output path to save all resultant files from this process
		:param training_data: Data to be used in building CCG SVM model
		:param instance_params: redundant parameters--unused in this build
		"""

		##
		## Paths and files to be used in this instance
		self.data_pair = data_pair
		self.prediction_path = prediction_path
		self.training_data = training_data
		self.instance_params = instance_params

		##
		## Build a classifier and class label hash-encoder for CCG SVM
		self.classifier, self.encoder = self.build_zygosity_model()

		"""
		Information/Error flags that exist within this class::
		--CCGZygDisconnect
		--CCGExpansionSkew
		--CCGPeakAmbiguous
		--CCGDensityAmbiguous
		--CCGRecallWarning
		--CCGPeakOOB
		--CAGConsensusSpreadWarning
		--CAGRecallWarning
		--FPSPDisconnect

		And data attributes that are used as output::
		PrimaryAllele = [CAGa, CCGb]
		SecondaryAllele = [CAGc, CCGd]
		PrimaryMosaicism = [<values>]
		SecondaryMosaicism = [<values>]
		"""
		self.prediction_confidence = 0
		self.cag_intermediate = [0,0]
		self.genotype_flags = {'PrimaryAllele':[0,0],
							   'SecondaryAllele':[0,0],
							   'PrimaryMosaicism':[],
							   'SecondaryMosaicism':[],
							   'CCGZygDisconnect':False,
							   'CCGExpansionSkew':False,
							   'CCGPeakAmbiguous':False,
							   'CCGDensityAmbiguous':False,
							   'CCGRecallWarning':False,
							   'CCGPeakOOB':False,
							   'CAGConsensusSpreadWarning':False,
							   'CAGRecallWarning':False,
							   'FPSPDisconnect':False}

		##
		## Unlabelled distributions to utilise for SVM prediction
		self.forward_distribution = self.scrape_distro(self.data_pair[0])
		self.reverse_distribution = self.scrape_distro(self.data_pair[1])

		"""
		!! Stage one !!
		Determine Zygosity of CCG from input distribution
		-- Aggregate CCG reads from 200x20 to 1x20
		-- Feed into SVM
		-- Compare results between forward and reverse (reverse takes priority)
		"""
		self.forwardccg_aggregate = self.distribution_collapse(self.forward_distribution)
		self.reverseccg_aggregate = self.distribution_collapse(self.reverse_distribution)
		self.zygosity_state = self.predict_zygstate()

		"""
		!! Stage two !!
		Determine CCG Peak(s)/Genotype(s) via 2-Pass Algorithm
		Run first attempt with no clauses; if pass, continue to next stage
		However, if something fails, a loop will trigger until the function passes
		"""
		ccg_failstate, ccg_genotype = self.determine_ccg_genotype()
		while ccg_failstate:
			self.genotype_flags['CCGRecallWarning'] = True
			ccg_failstate, ccg_genotype = self.determine_ccg_genotype(threshold_bias=True)

		self.genotype_flags['PrimaryAllele'][1] = ccg_genotype[0]
		self.genotype_flags['SecondaryAllele'][1] = ccg_genotype[1]

		"""
		!! Stage three !!
		Now we have identified the CCG peaks (successfully), we can investigate the appropriate
		CAG distributions for these CCG distribution(s). The same generic functions will be called
		for CAG determination, and results from CCG and CAG are combined to produce a genotype for this sample
		"""
		cag_failstate, cag_genotype = self.determine_cag_genotype()
		while cag_failstate:
			self.genotype_flags['CAGRecallWarning'] = True
			cag_failstate, cag_genotype = self.determine_cag_genotype(threshold_bias=True)

		self.genotype_flags['PrimaryAllele'][0] = cag_genotype[0]
		self.genotype_flags['SecondaryAllele'][0] = cag_genotype[1]

		"""
		!! Stage four !!
		Simple Somatic Mosaicism calculations are done here
		Append all information to the report to be returned to shd.sherpa for output
		"""
		self.genotype_flags['PrimaryMosaicism'] = self.somatic_calculations(self.genotype_flags['PrimaryAllele'])
		self.genotype_flags['SecondaryMosaicism'] = self.somatic_calculations(self.genotype_flags['SecondaryAllele'])
		self.gtype_report = self.generate_report()

	def build_zygosity_model(self):
		"""
		Function to build a SVM (wrapped into OvO class) for determining CCG zygosity
		:return: svm object wrapped into OvO, class-label hash-encoder object
		"""

		##
		## Classifier object and relevant parameters for our CCG prediction
		svc_object = svm.LinearSVC(C=1.0, loss='squared_hinge', penalty='l2', dual=False,
								   tol=1e-4, multi_class='crammer_singer', fit_intercept=True,
								   intercept_scaling=1, verbose=0, random_state=0, max_iter=-1)

		##
		## Take raw training data (CCG zygosity data) into DataLoader model object
		traindat_ccg_collapsed = self.training_data['CollapsedCCGZygosity']
		traindat_descriptionfi = self.training_data['GenericDescriptor']
		traindat_model = DataLoader(traindat_ccg_collapsed, traindat_descriptionfi).load_model()

		##
		## Model data fitting to SVM
		X = preprocessing.normalize(traindat_model.DATA)
		Y = traindat_model.TARGET
		ovo_svc = OneVsOneClassifier(svc_object).fit(X,Y)
		encoder = traindat_model.ENCDR

		##
		## Return the fitted OvO(SVM) and Encoder
		return ovo_svc, encoder

	@staticmethod
	def scrape_distro(distributionfi):
		"""
		Function to take the aligned read-count distribution from CSV into a numpy array
		:param distributionfi:
		:return: np.array(data_from_csv_file)
		"""

		##
		## Open CSV file with information within; append to temp list
		## Scrape information, cast to np.array(), return
		placeholder_array = []
		with open(distributionfi) as dfi:
			source = csv.reader(dfi, delimiter=',')
			next(source) #skip header
			for row in source:
				placeholder_array.append(int(row[2]))
			dfi.close()
		unlabelled_distro = np.array(placeholder_array)
		return unlabelled_distro

	@staticmethod
	def distribution_collapse(distribution_array):
		"""
		Function to take a full 200x20 array (struc: CAG1-200,CCG1 -- CAG1-200CCG2 -- etc CCG20)
		and aggregate all CAG values for each CCG
		:param distribution_array:
		:return: 1x20D np(array)
		"""

		##
		## Ensure there are 20CCG bins in our distribution
		try: ccg_arrays = np.split(distribution_array, 20)
		except ValueError: raise ValueError('{}{}{}{}'.format(clr.red,'shd__ ',clr.end,'Unable to split array into 20 CCG bins.'))

		##
		## Aggregate each CCG
		ccg_counter = 1
		collapsed_array = []
		for ccg_array in ccg_arrays:
			collapsed_array.append(np.sum(ccg_array))
			ccg_counter+=1
		return np.asarray(collapsed_array)

	def predict_zygstate(self):
		"""
		Function which takes the newly collapsed CCG distribution and executes SVM prediction
		to determine the zygosity state of this sample's CCG value(s). Data is reshaped
		and normalised to ensure more reliable results. A check is executed between the results of
		forward and reverse zygosity; if a match, great; if not, not explicitly bad but inform user.
		:return: zygosity[2:-2] (trimming unrequired characters)
		"""

		##
		## Reshape the input distribution so SKL doesn't complain about 1D vectors
		## Normalise data in addition; cast to float64 for this to be permitted
		forward_reshape = preprocessing.normalize(np.float64(self.forwardccg_aggregate.reshape(1,-1)))
		reverse_reshape = preprocessing.normalize(np.float64(self.reverseccg_aggregate.reshape(1,-1)))

		##
		## Predict the zygstate of these reshapen, noramlised 20D CCG arrays using SVM object earlier
		## Results from self.classifier are #encoded; so convert with our self.encoder.inverse_transform
		forward_zygstate = str(self.encoder.inverse_transform(self.classifier.predict(forward_reshape)))
		reverse_zygstate = str(self.encoder.inverse_transform(self.classifier.predict(reverse_reshape)))

		##
		## We only particularly care about the reverse zygosity (CCG reads are higher quality in reverse data)
		## However, for a QoL metric, compare fw/rv results. If match, good! If not, who cares!
		if not forward_zygstate == reverse_zygstate:
			self.genotype_flags['CCGZygDisconnect'] = True
			return reverse_zygstate[2:-2]
		else:
			self.genotype_flags['CCGZyg_disconnect'] = False
			return reverse_zygstate[2:-2]

	def update_flags(self, target_updates):
		"""
		Function that will take a list of flags that were raised from the 2-Pass algorithm
		and update this pipeline's instance of self.genotype_flags accordingly.
		This allows us to keep a current state-of-play of this sample's prediction.
		:param target_updates: List of flags from the 2-Pass algorithm
		:return: None
		"""

		for update_key, update_value in target_updates.iteritems():
			for initial_key, initial_value in self.genotype_flags.iteritems():
				if initial_key == update_key:
					self.genotype_flags[initial_key] = update_value

	def determine_ccg_genotype(self, fail_state=False, threshold_bias=False):
		"""
		Function to determine the genotype of this sample's CCG alleles
		Ideally this function will be called one time, but where exceptions occur
		it may be re-called with a lower quality threshold -- inform user when this occurs
		:param fail_state: optional flag for re-calling when a previous call failed
		:param threshold_bias: optional flag for lowering FOD threshold when a previous called failed
		:return: failure state, CCG genotype data ([None,X],[None,Y])
		"""
		peak_target = 0
		if self.zygosity_state == 'HOMO': peak_target = 1
		if self.zygosity_state == 'HETERO': peak_target = 2

		##
		## Create object for 2-Pass algorithm to use with CCG
		graph_parameters = [20, 'CCGDensityEstimation.png', 'CCG Density Distribution', ['Read Count', 'Bin Density']]
		ccg_inspector = SequenceTwoPass(prediction_path=self.prediction_path,
										input_distribution=self.reverseccg_aggregate,
										peak_target=peak_target,
										graph_parameters=graph_parameters)

		"""
		!! Sub-Stage one !!
		Now that we've made an object with the settings for this instance..
		Density estimation of the CCG distribution..
		Get warnings encountered by this instance of SequenceTwoPass
		Update equivalent warning flags within GenotypePrediction
		"""
		first_pass = ccg_inspector.density_estimation(plot_flag = True)
		density_warnings = ccg_inspector.get_warnings()
		self.update_flags(density_warnings)

		"""
		!! Sub-Stage two !!
		Now we have our estimates from the KDE sub-stage, we can use these findings
		in our FOD peak identification for more specific peak calling and thus, genotyping
		"""
		fod_param = [[0,19,20],'CCG Peaks',['CCG Value', 'Read Count'], 'CCGPeakDetection.png']
		fod_failstate, second_pass = ccg_inspector.differential_peaks(first_pass, fod_param, threshold_bias)
		while fod_failstate:
			fod_failstate, second_pass = ccg_inspector.differential_peaks(first_pass, fod_param, threshold_bias, fod_recall=True)
		differential_warnings = ccg_inspector.get_warnings()
		self.update_flags(differential_warnings)

		##
		## Check if First Pass Estimates == Second Pass Results
		## If there is a mismatch, genotype calling has failed and thus re-call will be required
		first_pass_estimate = [first_pass['PrimaryPeak'],first_pass['SecondaryPeak']]
		second_pass_estimate = [second_pass['PrimaryPeak'], second_pass['SecondaryPeak']]

		if not first_pass_estimate == second_pass_estimate or len(second_pass_estimate)>len(first_pass_estimate):
			fail_state = True

		##
		## Return whether this process passed or not, and the CURRENTLY PLACEHOLDER gtype
		return fail_state, second_pass_estimate

	@staticmethod
	def split_cag_target(input_distribution, ccg_target):
		"""
		Function to gather the relevant CAG distribution for the specified CCG value
		We gather this information from the forward distribution of this sample pair as CCG reads are
		of higher quality in the forward sequencing direction.
		We split the entire fw_dist into contigs/bins for each CCG (4000 -> 200*20)
		:param input_distribution: input forward distribution (4000d)
		:param ccg_target: target value we want to select the 200 values for
		:return: the sliced CAG distribution for our specified CCG value
		"""

		cag_split = [input_distribution[i:i+200] for i in xrange(0, len(input_distribution), 200)]
		distribution_dict = {}
		for i in range(0, len(cag_split)):
			distribution_dict['CCG'+str(i+1)] = cag_split[i]

		current_target_distribution = distribution_dict['CCG' + str(ccg_target)]
		return current_target_distribution

	def determine_cag_genotype(self, fail_state=False, threshold_bias=False):
		"""
		Function to determine the genotype of this sample's CAG alleles
		Ideally this function will be called one time, but where exceptions occur,
		it may be re-caled with a lower quality-threshold -- inform user when this occurs

		If CCG was homozygous (i.e. one CCG distro) -- there will be 2 CAG peaks to investigate
		If CCG was heterozygous (i.e. two CCG distro) -- there will be 1 CAG peak in each CCG to investigate

		:param fail_state: optional flag for re-calling when a previous call failed
		:param threshold_bias: optional flag for lowering FOD threshold when a previous call failed
		:return: failure state, CAG genotype data ([X,None],[Y,None])
		"""

		##
		## Set up distributions we require to investigate
		## If Homozygous, we have one CCG distribution that will contain 2 CAG peaks to investigate
		## If Heterozygous, we have two CCG distributions, each with 1 CAG peak to investigate
		peak_target = 0
		target_distribution = {}
		if self.zygosity_state == 'HOMO':
			peak_target = 2
			cag_target = self.split_cag_target(self.forward_distribution, self.genotype_flags['PrimaryAllele'][1])
			target_distribution[self.genotype_flags['PrimaryAllele'][1]] = cag_target
		if self.zygosity_state == 'HETERO':
			peak_target = 1
			cag_target_major = self.split_cag_target(self.forward_distribution, self.genotype_flags['PrimaryAllele'][1])
			cag_target_minor = self.split_cag_target(self.forward_distribution, self.genotype_flags['SecondaryAllele'][1])
			target_distribution[self.genotype_flags['PrimaryAllele'][1]] = cag_target_major
			target_distribution[self.genotype_flags['SecondaryAllele'][1]] = cag_target_minor

		##
		## Now iterate over our scraped distributions with our 2 pass algorithm
		for cag_key, distro_value in target_distribution.iteritems():

			##
			## Generate KDE graph parameters
			## Generate CAG inspector Object for 2Pass-Algorithm
			graph_parameters = [20, 'CAG'+str(cag_key)+'DensityEstimation.png', 'CAG Density Distribution', ['Read Count', 'Bin Density']]
			cag_inspector = SequenceTwoPass(prediction_path=self.prediction_path,
											input_distribution=distro_value,
											peak_target=peak_target,
											graph_parameters=graph_parameters)

			##TODO CAG Spread investigation, do we bother?

			"""
			!! Sub-stage one !!
			Now that we've made an object with the settings for this instance..
			Density estimation of the CCG distribution..
			Get warnings encountered by this instance of SequenceTwoPass
			Update equivalent warning flags within GenotypePrediction
			"""
			first_pass = cag_inspector.density_estimation(plot_flag=False)

			"""
			!! Sub-stage two !!
			Now we have our estimates from the KDE sub-stage, we can use these findings
			in our FOD peak identification for more specific peak calling and thus, genotyping
			"""
			fod_param = [[0,199,200],'CAG Peaks',['CAG Value', 'Read Count'], 'CAG'+str(cag_key)+'PeakDetection.png']
			fod_failstate, second_pass = cag_inspector.differential_peaks(first_pass, fod_param, threshold_bias)
			while fod_failstate:
				fod_failstate, second_pass = cag_inspector.differential_peaks(first_pass, fod_param, threshold_bias, fod_recall=True)

			##
			## Concatenate results into a sample-wide genotype format
			first_pass_estimate = [first_pass['PrimaryPeak'], first_pass['SecondaryPeak']]
			second_pass_estimate = [second_pass['PrimaryPeak'], second_pass['SecondaryPeak']]

			if not first_pass_estimate == second_pass_estimate or len(second_pass_estimate)>len(first_pass_estimate):
				fail_state = True

			##
			## Ensure the correct CAG is assigned to the appropriate CCG
			if self.zygosity_state == 'HOMO':
				if cag_key == self.genotype_flags['PrimaryAllele'][1]:
					self.cag_intermediate[0] = second_pass_estimate[0]
					self.cag_intermediate[1] = second_pass_estimate[1]
			if self.zygosity_state == 'HETERO':
				if cag_key == self.genotype_flags['PrimaryAllele'][1]:
					self.cag_intermediate[0] = second_pass_estimate[0]
				if cag_key == self.genotype_flags['SecondaryAllele'][1]:
					self.cag_intermediate[1] = second_pass_estimate[0]

		##
		## Generate object and return
		cag_genotype = [self.cag_intermediate[0],self.cag_intermediate[1]]
		return fail_state, cag_genotype

	def somatic_calculations(self, genotype):
		"""
		Function for basic somatic mosaicism calculations; featureset will be expanded upon later
		For now; N-1 / N, N+1 / N calculations are executed on arranged contigs where the N value is known
		(from genotype prediction -- assumed to be correct)
		In addition, the read count distribution for the forward and reverse reads in a sample pair are both
		aligned so that their N value is in the same position; lets end-user investigate manual distribution
		data quality etc.
		:param genotype: value of predicted genotype from the SVM/2PA stages of GenotypePrediction()
		:return: mosaicism_values; results of simple sommos calculations :))))))
		"""

		##
		## Create mosaicism investigator object to begin calculation prep
		## Takes raw 200x20 dist and slices into 20 discrete 200d arrays
		## Orders into a dataframe with CCG<val> labels
		## Scrapes appropriate values for SomMos calculations
		mosaicism_object = MosaicismInvestigator(genotype, self.forward_distribution)
		ccg_slices = mosaicism_object.chunks(200)
		ccg_ordered = mosaicism_object.arrange_chunks(ccg_slices)
		allele_values = mosaicism_object.get_nvals(ccg_ordered, genotype)

		##
		## With these values, we can calculate and return
		allele_calcs = mosaicism_object.calculate_mosaicism(allele_values)

		##
		## Generate a padded distribution (aligned to N=GTYPE)
		padded_distro = mosaicism_object.distribution_padder(ccg_ordered, genotype)

		##
		## Combine calculation dictionary and distribution into object, return
		mosaicism_values = [allele_calcs, padded_distro]
		return mosaicism_values

	def generate_report(self):
		"""
		Function which will, eventually, calculate the confidence score of this genotype prediction
		by taking into account flags raised, and meta-data about the current sample distribution etc
		:return: For now, a list of report flags. eventually, probably a dictionary with more info within
		"""

		##
		## TODO Calculate genotype confidence score based on flags, raw read count, density estimation clarity..
		## TODO ..somatic mosaicism, how many times particular functions were re-called..
		## TODO other factors to involve into the confidence scoring?

		report = [self.genotype_flags['PrimaryAllele'],
				  self.genotype_flags['SecondaryAllele'],
				  self.genotype_flags['CCGZygDisconnect'],
				  self.genotype_flags['CCGExpansionSkew'],
				  self.genotype_flags['CCGPeakAmbiguous'],
				  self.genotype_flags['CCGDensityAmbiguous'],
				  self.genotype_flags['CCGRecallWarning'],
				  self.genotype_flags['CCGPeakOOB'],
				  self.genotype_flags['CAGRecallWarning'],
				  self.genotype_flags['CAGConsensusSpreadWarning'],
				  self.genotype_flags['FPSPDisconnect'],
				  self.genotype_flags['PrimaryMosaicism'],
				  self.genotype_flags['SecondaryMosaicism']]

		return report

	def get_report(self):
		"""
		Function to just return the report for this class object from the point of calling
		:return: a report. wow
		"""
		return self.gtype_report

class SequenceTwoPass:
	def __init__(self, prediction_path, input_distribution, peak_target, graph_parameters):
		"""
		Class that will be used as an object for each genotyping stage of the GenotypePrediction pipe
		Each function within this class has it's own doctstring for further explanation
		This class is called into an object for each of CCG/CAG deterministic stages

		:param prediction_path: Output path to save all resultant files from this process
		:param input_distribution: Distribution to put through the two-pass (CAG or CCG..)
		:param peak_target: Number of peaks we expect to see in this current distribution
		:param graph_parameters: Parameters (names of axes etc..) for saving results to graph
		"""

		##
		## Variables for this instance of this object
		self.prediction_path = prediction_path
		self.input_distribution = input_distribution
		self.peak_target = peak_target
		self.bin_count = graph_parameters[0]
		self.filename = graph_parameters[1]
		self.graph_title = graph_parameters[2]
		self.axes = graph_parameters[3]
		self.instance_parameters = {}

		##
		## Potential warnings raised in this instance
		self.density_ambiguity = False
		self.expansion_skew = False
		self.peak_ambiguity = False

	def histogram_generator(self, filename, graph_title, axes, plot_flag):
		"""
		Generate histogrm of kernel density estimation for this instance of 2PA
		:param filename: Filename for graph to be saved as..
		:param graph_title: self explanatory
		:param axes: self explanatory
		:param plot_flag: CCG? Plot KDE. CAG? Don't.
		:return: histogram, bins
		"""

		##
		## Generate KDE histogram and plot to graph
		hist, bins = np.histogram(self.input_distribution, bins=self.bin_count, density=True)
		if plot_flag:
			plt.figure(figsize=(10,6))
			bin_width = 0.7 * (bins[1] - bins[0])
			center = (bins[:-1] + bins[1:]) / 2
			plt.title(graph_title)
			plt.xlabel(axes[0])
			plt.ylabel(axes[1])
			plt.bar(center, hist, width=bin_width)
			plt.savefig(os.path.join(self.prediction_path, filename), format='png')
			plt.close()

		##
		## Check the number of densities that exist within our histogram
		## If there are many (>2) values that are very low density (i.e. relevant)
		## then raise the flag for density ambiguity -- there shouldn't be many values so issue with data
		density_frequency = Counter(hist)
		for key, value in density_frequency.iteritems():
			if not key == np.float64(0.0) and value > 2:
				self.density_ambiguity = True

		##
		## Return histogram and bins to where this function was called
		return hist, bins

	@staticmethod
	def peak_clarity(peak_target, hist_list, major_bin, major_sparsity, minor_bin=None, minor_sparsity=None):
		"""
		Function to determine how clean a peak is (homo/hetero)
		Look at densities around each supposed peak, and if the value is close then increment a count
		if the count is above a threshold, return False to indicate failure (and raise a flag)
		:param peak_target: hetero/homo
		:param hist_list: list of histogram under investigation
		:param major_bin: bin of hist of major peak
		:param major_sparsity: sparsity value of that
		:param minor_bin: bin of hist of minor peak
		:param minor_sparsity: sparsity value of that
		:return: True/False
		"""

		clarity_count = 0
		if peak_target == 1:
			major_slice = hist_list[major_bin - 2:major_bin + 2]
			for density in major_slice:
				if np.isclose(major_sparsity, density):
					clarity_count += 1
			if clarity_count > 3:
				return False
		if peak_target == 2:
			major_slice = hist_list[major_bin - 2:major_bin + 2]
			minor_slice = hist_list[minor_bin - 2:minor_bin + 2]
			for density in major_slice:
				if np.isclose(major_sparsity, density):
					clarity_count += 1
			for density in minor_slice:
				if np.isclose(minor_sparsity, density):
					clarity_count += 1
			if clarity_count > 5:
				return False
		return True

	def density_estimation(self, plot_flag):
		"""
		Denisity estimate for a given input distribution (self.input_distribution)
		Use KDE to determine roughly where peaks should be located, peak distances, etc
		Plot graphs for visualisation, return information to origin of call
		:param plot_flag: do we plot a graph or not? (CCG:Yes,CAG:No)
		:return: {dictionary of estimated attributes for this input}
		"""

		##
		## Set up variables for this instance's run of density estimation
		## and generate a dictionary to be modified/returned
		distro_list = list(self.input_distribution)
		major_estimate = None; minor_estimate = None
		peak_distance = None; peak_threshold = None
		estimated_attributes = {'PrimaryPeak':major_estimate,
								'SecondaryPeak':minor_estimate,
								'PeakDistance':peak_distance,
								'PeakThreshold':peak_threshold}

		##
		## Begin density estimation!
		## By default, runs in heterozygous assumption
		## If instance requires homozygous then tailor output instead of re-running
		major_estimate = max(self.input_distribution); major_index = distro_list.index(major_estimate)
		minor_estimate = max(n for n in distro_list if n!=major_estimate); minor_index = distro_list.index(minor_estimate)

		##
		## Check that N-1 of <MAJOR> is not <MINOR> (i.e. slippage)
		## If so, correct for minor == 3rd highest and raise error flag!
		if minor_index == major_index-1:
			literal_minor_estimate = max(n for n in distro_list if n!=major_estimate and n!=minor_estimate)
			literal_minor_index = distro_list.index(literal_minor_estimate)
			minor_estimate = literal_minor_estimate
			minor_index = literal_minor_index
			self.expansion_skew = True

		##
		## Actual execution of the Kernel Density Estimation histogram
		hist, bins = self.histogram_generator(self.filename, self.graph_title, self.axes, plot_flag)
		hist_list = list(hist)

		##
		## Determine which bin in the density histogram our estimate values reside within
		## -1 because for whatever reason np.digitize adds one to the literal index
		major_estimate_bin = np.digitize([major_estimate], bins)-2
		minor_estimate_bin = np.digitize([minor_estimate], bins)-1

		##
		## Relevant densities depending on zygosity of the current sample
		major_estimate_sparsity = None; minor_estimate_sparsity = None
		if self.peak_target == 1:
			major_estimate_sparsity = min(n for n in hist if n!=0)
			minor_estimate_sparsity = min(n for n in hist if n!=0)
			peak_distance = 0
			if not self.peak_clarity(self.peak_target, hist_list, major_estimate_bin, major_estimate_sparsity):
				self.peak_ambiguity = True
		if self.peak_target == 2:
			major_estimate_sparsity = min(n for n in hist if n!=0)
			minor_estimate_sparsity = min(n for n in hist if n!=0 and n!=major_estimate_sparsity)
			peak_distance = np.absolute(major_index - minor_index)
			if not self.peak_clarity(self.peak_target, hist_list, major_estimate_bin, major_estimate_sparsity, minor_estimate_bin, minor_estimate_sparsity):
				self.peak_ambiguity = True

		##
		## Check for multiple low densities in distribution
		fuzzy_count = 0
		for density in hist_list:
			if np.isclose(major_estimate_sparsity, density):
				fuzzy_count+=1
		if fuzzy_count > 3:
			self.density_ambiguity = True

		##
		## Determine Thresholds for this instance sample
		## TODO MORE THRESHOLD MODIFIERS
		peak_threshold = 0.50
		if self.expansion_skew: peak_threshold -= 0.05
		if self.density_ambiguity: peak_threshold -= 0.075
		if self.peak_ambiguity: peak_threshold -= 0.10

		##
		## Preparing estimated attributes for return
		if self.peak_target == 1:
			estimated_attributes['PrimaryPeak'] = major_index+1
			estimated_attributes['SecondaryPeak'] = major_index+1
		if self.peak_target == 2:
			estimated_attributes['PrimaryPeak'] = major_index+1
			estimated_attributes['SecondaryPeak'] = minor_index+1
		estimated_attributes['PeakDistance'] = peak_distance
		estimated_attributes['PeakThreshold'] = peak_threshold

		return estimated_attributes

	def differential_peaks(self, first_pass, fod_params, threshold_bias, fail_state=False, fod_recall=False):
		"""
		Function which takes in parameters gathered from density estimation
		and applies them to a First Order Differential peak detection algorithm
		to more precisely determine the peak (and thus, genotype) of a sample
		:param first_pass: Dictionary of results from KDE
		:param fod_params: Parameters for graphs made in this function
		:param threshold_bias: Bool for whether this call is a re-call or not (lower threshold if True)
		:param fail_state: did this FOD fail or not?
		:param fod_recall: do we need to do a local re-call?
		:return: dictionary of results from KDE influenced FOD
		"""

		##
		## Get Peak information from the KDE dictionary
		## If threshold_bias == True, this is a recall, so lower threshold
		## but ensure the threshold stays within the expected ranges
		peak_distance = first_pass['PeakDistance']
		peak_threshold = first_pass['PeakThreshold']
		if threshold_bias or fod_recall:
			first_pass['PeakThreshold'] -= 0.10
			peak_threshold -= 0.10
			peak_threshold = max(peak_threshold,0.05)

		##
		## Graph Parameters expansion
		linspace_dimensionality = fod_params[0]
		graph_title = fod_params[1]
		axes = fod_params[2]
		filename = fod_params[3]

		##
		## Create planar space for plotting
		## Send paramters to FOD
		## Increment results by 1 (to resolve 0 indexing)
		x = np.linspace(linspace_dimensionality[0], linspace_dimensionality[1], linspace_dimensionality[2])
		y = np.asarray(self.input_distribution)
		peak_indexes = peakutils.indexes(y, thres=peak_threshold, min_dist=peak_distance-1)
		fixed_indexes = peak_indexes+1

		##
		## Plot Graph!
		## TODO plot peak label onto graph for QOL
		plt.figure(figsize=(10,6))
		plt.title(graph_title)
		plt.xlabel(axes[0])
		plt.ylabel(axes[1])
		pplot(x,y,peak_indexes)
		plt.savefig(os.path.join(self.prediction_path,filename), format='png')
		plt.close()

		##
		## Try to assign peaks to the appropriate indexes
		## If there is an IndexError, we have too few peaks called
		## I.E. failure, and we need to lower the threshold (re-call)
		if self.peak_target == 1:
			try:
				first_pass['PrimaryPeak'] = fixed_indexes[0]
				first_pass['SecondaryPeak'] = fixed_indexes[0]
			except IndexError:
				fail_state = True

		if self.peak_target == 2:
			try:
				first_pass['PrimaryPeak'] = fixed_indexes[0]
				first_pass['SecondaryPeak'] = fixed_indexes[1]
			except IndexError:
				fail_state = True

		return fail_state, first_pass

	def get_warnings(self):
		"""
		Function which generates a dictionary of warnings encountered in this instance of SequenceTwoPass
		Dictionary is later sorted into the GenotypePrediction equivalency for returning into a report file
		:return: {warnings}
		"""

		return {'ExpansionSkew':self.expansion_skew,
				'PeakAmbiguity':self.peak_ambiguity,
				'DensityAmbiguity':self.density_ambiguity}


class MosaicismInvestigator:
	def __init__(self, genotype, distribution):
		"""
		A class which is called when the functions within are required for somatic mosaicism calculations
		As of now there is only a basic implementation of somatic mosaicism studies but it's WIP
		"""

		self.genotype = genotype
		self.distribution = distribution

	def chunks(self, n):
		"""
		Function which takes an entire sample's distribution (200x20) and split into respective 'chunks'
		I.E. slice one distribution into contigs for each CCG (200x1 x 20)
		:param n: number to slice the "parent" distribution into
		:return: CHUNKZ
		"""

		for i in xrange(0, len(self.distribution), n):
			yield self.distribution[i:i + n]

	@staticmethod
	def arrange_chunks(ccg_slices):
		"""
		Function which takes the sliced contig chunks and orders them into a dataframe for ease of
		interpretation later on in the application. Utilises pandas for the dataframe class since
		it's the easiest to use.
		:param ccg_slices: The sliced CCG contigs
		:return: df: a dataframe which is ordered with appropriate CCG labels.
		"""

		arranged_rows = []
		for ccg_value in ccg_slices:
			column = []
			for i in range(0, len(ccg_value)):
				column.append(ccg_value[i])
			arranged_rows.append(column)

		df = pd.DataFrame({'CCG1': arranged_rows[0], 'CCG2': arranged_rows[1], 'CCG3': arranged_rows[2],
						   'CCG4': arranged_rows[3], 'CCG5': arranged_rows[4], 'CCG6': arranged_rows[5],
						   'CCG7': arranged_rows[6], 'CCG8': arranged_rows[7], 'CCG9': arranged_rows[8],
						   'CCG10': arranged_rows[9], 'CCG11': arranged_rows[10], 'CCG12': arranged_rows[11],
						   'CCG13': arranged_rows[12], 'CCG14': arranged_rows[13], 'CCG15': arranged_rows[14],
						   'CCG16': arranged_rows[15], 'CCG17': arranged_rows[16], 'CCG18': arranged_rows[17],
						   'CCG19': arranged_rows[18], 'CCG20': arranged_rows[19]})

		return df

	@staticmethod
	def get_nvals(df, input_allele):
		"""
		Function to take specific CCG contig sub-distribution from dataframe
		Extract appropriate N-anchored values for use in sommos calculations
		:param df: input dataframe consisting of all CCG contig distributions
		:param input_allele: genotype derived from GenotypePrediction (i.e. scrape target)
		:return: allele_nvals: dictionary of n-1/n/n+1
		"""
		allele_nvals = {}
		cag_value = input_allele[0]
		ccgframe = df['CCG'+str(input_allele[1])]

		try:
			nminus = str(ccgframe[int(cag_value)-2])
			nvalue = str(ccgframe[int(cag_value)-1])
			nplus = str(ccgframe[int(cag_value)])
		except KeyError:
			log.info('{}{}{}{}'.format(clr.red,'shd__ ',clr.end,'N-Value scraping Out of Bounds.'))

		allele_nvals['NMinusOne'] = nminus
		allele_nvals['NValue'] = nvalue
		allele_nvals['NPlusOne'] = nplus

		return allele_nvals

	@staticmethod
	def calculate_mosaicism(allele_values):
		"""
		Function to execute the actual calculations
		Perhaps float64 precision is better? Might not matter for us
		Also required to add additional calculations here to make the 'suite' more robust
		:param allele_values: dictionary of this sample's n-1/n/n+1
		:return: dictionary of calculated values
		"""

		nmo = allele_values['NMinusOne']
		n = allele_values['NValue']
		npo = allele_values['NPlusOne']
		nmo_over_n = 0
		npo_over_n = 0

		try:
			nmo_over_n = int(nmo) / int(n)
			npo_over_n = int(npo) / int(n)
		except ZeroDivisionError:
			log.info('{}{}{}{}'.format(clr.red,'shd__ ',clr.end,' Divide by 0 attempted in Mosaicism Calculation.'))

		calculations = {'NMinusOne':nmo,'NValue':n,'NPlusOne':npo,'NMinusOne-Over-N': nmo_over_n, 'NPlusOne-Over-N': npo_over_n}
		return calculations

	@staticmethod
	def distribution_padder(ccg_dataframe, genotype):
		"""
		Function to ensure all distribution's N will be anchored to the same position in a file
		This is to allow the end user manual insight into the nature of the data (requested for now)
		E.G. larger somatic mosaicism spreads/trends in a distribution + quick sample-wide comparison
		:param ccg_dataframe: dataframe with all CCG contigs
		:param genotype: genotype derived from GenotypePrediction i.e. scrape target
		:return: distribution with appropriate buffers on either side so that N is aligned to same position as all others
		"""
		unpadded_distribution = list(ccg_dataframe['CCG'+str(genotype[1])])
		n_value = genotype[0]

		anchor = 203
		anchor_to_left = anchor - n_value
		anchor_to_right	= anchor_to_left + 200
		left_buffer = ['-'] * anchor_to_left
		right_buffer = ['-'] * (403-anchor_to_right)
		padded_distribution = left_buffer + unpadded_distribution + right_buffer

		return padded_distribution