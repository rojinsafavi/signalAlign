#!/usr/bin/env python
"""Train HMMs for alignment of signal data from the MinION
"""
from __future__ import print_function, division
import sys
import h5py as h5
sys.path.append("../")
from multiprocessing import Process, Queue, current_process, Manager
from subprocess import check_output
from signalAlignLib import *
from argparse import ArgumentParser
from random import shuffle
from shutil import copyfile


def parse_args():
    parser = ArgumentParser (description=__doc__)

    parser.add_argument('--file_directory', '-d', action='append', default=None,
                        dest='files_dir', required=False, type=str,
                        help="directories with fast5 files to train on")
    parser.add_argument('--ref', '-r', action='store', default=None,
                        dest='ref', required=False, type=str,
                        help="location of refrerence sequence in FASTA")
    parser.add_argument('--output_location', '-o', action='store', dest='out', default=None,
                        required=False, type=str,
                        help="directory to put the trained model, and use for working directory.")
    parser.add_argument('--iterations', '-i', action='store', dest='iter', default=10,
                        required=False, type=int, help='number of iterations to perform')
    parser.add_argument('--train_amount', '-a', action='store', dest='amount', default=15,
                        required=False, type=int,
                        help="limit the total length of sequence to use in training (batch size).")
    parser.add_argument('--diagonalExpansion', '-e', action='store', dest='diag_expansion', type=int,
                        required=False, default=None, help="number of diagonals to expand around each anchor")
    parser.add_argument('--constraintTrim', '-m', action='store', dest='constraint_trim', type=int,
                        required=False, default=None, help='amount to remove from an anchor constraint')
    parser.add_argument('--threshold', '-t', action='store', dest='threshold', type=float, required=False,
                        default=0.5, help="posterior match probability threshold")
    parser.add_argument('--verbose', action='store_true', default=False, dest='verbose')
    parser.add_argument('--emissions', action='store_true', default=False, dest='emissions',
                        help="Flag to train emissions, False by default")
    parser.add_argument('--transitions', action='store_true', default=False, dest='transitions',
                        help='Flag to train transitions, False by default')

    parser.add_argument('--in_template_hmm', '-T', action='store', dest='in_T_Hmm',
                        required=False, type=str, default=None,
                        help="input HMM for template events, if you don't want the default")
    parser.add_argument('--in_complement_hmm', '-C', action='store', dest='in_C_Hmm',
                        required=False, type=str, default=None,
                        help="input HMM for complement events, if you don't want the default")
    parser.add_argument('---un-banded', '-ub', action='store_false', dest='banded',
                        default=True, help='flag, turn off banding')
    parser.add_argument('--jobs', '-j', action='store', dest='nb_jobs', required=False, default=4,
                        type=int, help="number of jobs to run concurrently")
    parser.add_argument('--stateMachineType', '-smt', action='store', dest='stateMachineType', type=str,
                        default="threeState", required=False,
                        help="StateMachine options: threeState, threeStateHdp")
    parser.add_argument('--templateHDP', '-tH', action='store', dest='templateHDP', default=None,
                        help="path to template HDP model to use")
    parser.add_argument('--complementHDP', '-cH', action='store', dest='complementHDP', default=None,
                        help="path to complement HDP model to use")

    parser.add_argument('--test', action='store_true', default=False, dest='test')

    # gibbs
    parser.add_argument('--samples', '-s', action='store', type=int, default=10000, dest='gibbs_samples')
    parser.add_argument('--thinning', '-th', action='store', type=int, default=100, dest='thinning')
    parser.add_argument('--min_assignments', action='store', type=int, default=30000, dest='min_assignments',
                        help="Do not initiate Gibbs sampling unless this many assignments have been accumulated")
    # only supervised training enabled right now
    #parser.add_argument('--degenerate', '-x', action='store', dest='degenerate', default="variant",
    #                    help="Specify degenerate nucleotide options: "
    #                         "variant -> {ACGT}, twoWay -> {CE} threeWay -> {CEO}")
    #parser.add_argument('-ambiguity_positions', '-p', action='store', required=False, default=None,
    #                    dest='substitution_file', help="Ambiguity positions")
    args = parser.parse_args()
    return args


def get_2d_length(fast5):
    read = h5.File(fast5, 'r')
    read_length = 0
    twoD_read_sequence_address = "/Analyses/Basecall_2D_000/BaseCalled_2D/Fastq"
    if not (twoD_read_sequence_address in read):
        print("This read didn't have a 2D read", fast5, end='\n', file=sys.stderr)
        read.close()
        return 0
    else:
        read_length = len(read[twoD_read_sequence_address][()].split()[2])
        read.close()
        return read_length


def cull_training_files(directories, training_amount):
    print("trainModels - culling training files.\n", end="", file=sys.stderr)

    training_files = []
    add_to_training_files = training_files.append

    # loop over the directories and collect reads for training
    for j, directory in enumerate(directories):
        fast5s = [x for x in os.listdir(directory) if x.endswith(".fast5")]
        shuffle(fast5s)
        total_amount = 0
        n = 0
        # loop over files and add them to training list, break when we have enough bases to complete a batch
        for i in xrange(len(fast5s)):
            add_to_training_files(directory + fast5s[i])
            n += 1
            total_amount += get_2d_length(directory + fast5s[i])
            if total_amount >= training_amount:
                break
        print("Culled {nb_files} training files, for {bases} from {dir}.".format(nb_files=len(training_files),
                                                                                 bases=total_amount,
                                                                                 dir=directory),
              end="\n", file=sys.stderr)

    return training_files


def get_expectations(work_queue, done_queue):
    try:
        for f in iter(work_queue.get, 'STOP'):
            alignment = SignalAlignment(**f)
            alignment.run(get_expectations=True)
    except Exception, e:
        done_queue.put("%s failed with %s" % (current_process().name, e.message))


def get_model(type, threshold, model_file):
    assert (type in ["threeState", "threeStateHdp"]), "Unsupported StateMachine type"
    # todo clean this up
    if type == "threeState":
        assert model_file is not None, "Need to have starting lookup table for {} HMM".format(type)
        model = ContinuousPairHmm(model_type=type)
        model.load_model(model_file=model_file)
        return model
    if type == "threeStateHdp":
        model = HdpSignalHmm(model_type=type, threshold=threshold)
        model.load_model(model_file=model_file)
        return model


def add_and_norm_expectations(path, files, model, hmm_file, update_transitions=False, update_emissions=False):
    if update_emissions is False and update_transitions is False:
        print("[trainModels] NOTICE: Training transitions by default\n", file=sys.stderr)
        update_transitions = True

    model.likelihood = 0
    files_added_successfully = 0
    files_with_problems = 0
    for f in files:
        try:
            success = model.add_expectations_file(path + f)
            os.remove(path + f)
            if success:
                files_added_successfully += 1
            else:
                files_with_problems += 1
        except Exception as e:
            print("Problem adding expectations file {file} got error {e}".format(file=path + f, e=e),
                  file=sys.stderr)
            os.remove(path + f)
            files_with_problems += 1
    model.normalize(update_transitions=update_transitions, update_emissions=update_emissions)
    model.write(hmm_file)
    model.running_likelihoods.append(model.likelihood)
    if type(model) is HdpSignalHmm:
        model.reset_assignments()
    print("[trainModels] NOTICE: Added {success} expectations files successfully, {problem} files had problems\n"
          "".format(success=files_added_successfully, problem=files_with_problems), file=sys.stderr)


def build_hdp(template_hdp_path, complement_hdp_path, template_assignments, complement_assignments, samples,
              burn_in, thinning, verbose=False):
    assert (template_assignments is not None) and (complement_assignments is not None), \
        "trainModels - ERROR: missing assignments"

    if verbose is True:
        verbose_flag = "--verbose "
    else:
        verbose_flag = ""

    command = "./buildHdpUtil {verbose}-v {tHdpP} -w {cHdpP} -E {tExpectations} -W {cExpectations} " \
              "-n {samples} -I {burnIn} -t {thinning}".format(tHdpP=template_hdp_path,
                                                              cHdpP=complement_hdp_path,
                                                              tExpectations=template_assignments,
                                                              cExpectations=complement_assignments,
                                                              samples=samples, burnIn=burn_in,
                                                              thinning=thinning,
                                                              verbose=verbose_flag)
    print("[trainModels] Running command:{}".format(command), file=sys.stderr)
    os.system(command)  # todo try checkoutput
    print("trainModels - built HDP.", file=sys.stderr)
    return


def main(args):
    # parse command line arguments
    args = parse_args()

    command_line = " ".join(sys.argv[:])
    print("Command Line: {cmdLine}\n".format(cmdLine=command_line), file=sys.stderr)

    start_message = """\n
    # Starting Baum-Welch training.
    # Directories with training files: {files_dir}
    # Each batch has {amount} bases, performing {iter} iterations.
    # Using reference sequence: {ref}
    # Writing trained models to: {outLoc}
    # Performing {iterations} iterations.
    # Using model: {model}
    # Using HDPs: {thdp} / {chdp}
    # Training emissions: {emissions}
    #        transitions: {transitions}
    \n
    """.format(files_dir=args.files_dir, amount=args.amount, ref=args.ref, outLoc=args.out, iter=args.iter,
               iterations=args.iter, model=args.stateMachineType, thdp=args.templateHDP, chdp=args.complementHDP,
               emissions=args.emissions, transitions=args.transitions)

    assert (args.files_dir is not None), "Need to specify which files to train on"
    assert (args.ref is not None), "Need to provide a reference file"
    assert (args.out is not None), "Need to know the working directory for training"

    if not os.path.isfile(args.ref):  # TODO make this is_fasta(args.ref)
        print("Did not find valid reference file", file=sys.stderr)
        sys.exit(1)

    print(start_message, file=sys.stdout)

    # make directory to put the files we're using files
    working_folder = FolderHandler()
    working_directory_path = working_folder.open_folder(args.out + "tempFiles_expectations")

    # make the plus and minus strand sequences
    plus_strand_sequence = working_folder.add_file_path("forward_reference.txt")
    minus_strand_sequence = working_folder.add_file_path("backward_reference.txt")

    make_temp_sequence(fasta=args.ref,
                       sequence_outfile=plus_strand_sequence,
                       rc_sequence_outfile=minus_strand_sequence)

    # index the reference for bwa
    print("signalAlign - indexing reference", file=sys.stderr)
    bwa_ref_index = get_bwa_index(args.ref, working_directory_path)
    print("signalAlign - indexing reference, done", file=sys.stderr)

    # the default lookup tables are the starting conditions for the model if we're starting from scratch
    # todo next make get default model function based on version inferred from reads
    template_model_path = "../../signalAlign/models/testModel_template.model" if \
        args.in_T_Hmm is None else args.in_T_Hmm
    complement_model_path = "../../signalAlign/models/testModel_complement.model" if \
        args.in_C_Hmm is None else args.in_C_Hmm

    assert os.path.exists(template_model_path) and os.path.exists(complement_model_path), \
        "Missing default lookup tables"

    # make the model objects, for the threeState model, we're going to parse the lookup table or the premade
    # model, for the HDP model, we just load the transitions
    template_model = get_model(type=args.stateMachineType, threshold=args.threshold, model_file=template_model_path)
    complement_model = get_model(type=args.stateMachineType, threshold=args.threshold, model_file=complement_model_path)

    # get the input HDP, if we're using it
    if args.stateMachineType == "threeStateHdp":
        assert (args.templateHDP is not None) and (args.complementHDP is not None), \
            "Need to provide serialized HDP files for this stateMachineType"
        assert (os.path.isfile(args.templateHDP)) and (os.path.isfile(args.complementHDP)),\
            "Could not find the HDP files"
        template_hdp = working_folder.add_file_path("{}".format(args.templateHDP.split("/")[-1]))
        complement_hdp = working_folder.add_file_path("{}".format(args.complementHDP.split("/")[-1]))
        copyfile(args.templateHDP, template_hdp)
        copyfile(args.complementHDP, complement_hdp)
    else:
        template_hdp = None
        complement_hdp = None

    # make some paths to files to hold the HMMs
    template_hmm = working_folder.add_file_path("template_trained.hmm")
    complement_hmm = working_folder.add_file_path("complement_trained.hmm")

    trained_models = [template_hmm, complement_hmm]

    untrained_models = [template_model_path, complement_model_path]

    for default_model, trained_model in zip(untrained_models, trained_models):
        assert os.path.exists(default_model), "Didn't find default model {}".format(default_model)
        copyfile(default_model, trained_model)
        assert os.path.exists(trained_model), "Problem copying default model to {}".format(trained_model)

    print("Starting {iterations} iterations.\n\n\t    Running likelihoods\ni\tTemplate\tComplement".format(
        iterations=args.iter), file=sys.stdout)

    # start iterating
    i = 0
    while i < args.iter:
        # first cull a set of files to get expectations on
        training_files = cull_training_files(args.files_dir, args.amount)

        # setup
        workers = args.nb_jobs
        work_queue = Manager().Queue()
        done_queue = Manager().Queue()
        jobs = []

        # get expectations for all the files in the queue
        for fast5 in training_files:
            alignment_args = {
                "forward_reference": plus_strand_sequence,
                "backward_reference": minus_strand_sequence,
                "path_to_EC_refs": None,
                "destination": working_directory_path,
                "stateMachineType": args.stateMachineType,
                "bwa_index": bwa_ref_index,
                "in_templateHmm": template_hmm,
                "in_complementHmm": complement_hmm,
                "in_templateHdp": template_hdp,
                "in_complementHdp": complement_hdp,
                "banded": args.banded,
                "sparse_output": False,
                "in_fast5": fast5,
                "threshold": args.threshold,
                "diagonal_expansion": args.diag_expansion,
                "constraint_trim": args.constraint_trim,
                "target_regions": None,
                "degenerate": None,
            }
            alignment = SignalAlignment(**alignment_args)
            alignment.run(get_expectations=True)
            #work_queue.put(alignment_args)

        for w in xrange(workers):
            p = Process(target=get_expectations, args=(work_queue, done_queue))
            p.start()
            jobs.append(p)
            work_queue.put('STOP')

        for p in jobs:
            p.join()

        done_queue.put('STOP')

        # load then normalize the expectations
        template_expectations_files = [x for x in os.listdir(working_directory_path)
                                       if x.endswith(".template.expectations")]

        complement_expectations_files = [x for x in os.listdir(working_directory_path)
                                         if x.endswith(".complement.expectations")]

        if len(template_expectations_files) > 0:
            add_and_norm_expectations(path=working_directory_path,
                                      files=template_expectations_files,
                                      model=template_model,
                                      hmm_file=template_hmm,
                                      update_emissions=args.emissions,
                                      update_transitions=args.transitions)

        if len(complement_expectations_files) > 0:
            add_and_norm_expectations(path=working_directory_path,
                                      files=complement_expectations_files,
                                      model=complement_model,
                                      hmm_file=complement_hmm,
                                      update_emissions=args.emissions,
                                      update_transitions=args.transitions)

        # Build HDP from last round of assignments
        if args.stateMachineType == "threeStateHdp" and args.emissions is True:
            assert isinstance(template_model, HdpSignalHmm) and isinstance(complement_model, HdpSignalHmm)
            if min(template_model.assignments_record[-1],
                   complement_model.assignments_record[-1]) < args.min_assignments:
                print("[trainModels] not enough assignments at iteration {}, continuing...".format(i),
                      file=sys.stderr)
                i -= 1
                pass
            else:
                total_assignments = max(template_model.assignments_record[-1], complement_model.assignments_record[-1])

                build_hdp(template_hdp_path=template_hdp, complement_hdp_path=complement_hdp,
                          template_assignments=template_hmm, complement_assignments=complement_hmm,
                          samples=args.gibbs_samples, thinning=args.thinning, burn_in=30 * total_assignments,
                          verbose=args.verbose)

        # log the running likelihood
        if len(template_model.running_likelihoods) > 0 and len(complement_model.running_likelihoods) > 0:
            print("{i}| {t_likelihood}\t{c_likelihood}".format(t_likelihood=template_model.running_likelihoods[-1],
                                                               c_likelihood=complement_model.running_likelihoods[-1],
                                                               i=i))
            if args.test and (len(template_model.running_likelihoods) >= 2) and \
                    (len(complement_model.running_likelihoods) >= 2):
                assert (template_model.running_likelihoods[-2] < template_model.running_likelihoods[-1]) and \
                       (complement_model.running_likelihoods[-2] < complement_model.running_likelihoods[-1]), \
                    "Testing: Likelihood error, went up"
        i += 1

    # if we're using HDP, trim the final Hmm (remove assignments)

    print("trainModels - finished training routine", file=sys.stdout)
    print("trainModels - finished training routine", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

