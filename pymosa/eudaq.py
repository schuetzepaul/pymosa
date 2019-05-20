#
# ------------------------------------------------------------
# Copyright (c) All rights reserved
# SiLab, Institute of Physics, University of Bonn
# ------------------------------------------------------------
#

'''
    This script connects pymosa to the EUDAQ 1.x data acquisition system.

    Three data taking modes are supported:
        0: legacy mode: 2 Mimosa26 frames are readout per external trigger,
        triggers are vetoed during readout (2 * 115 us)
        1: speed mode: 1 Mimosa26 frame is read out per trigger,
        triggers are vetoed during readout (1 * 115 us)
        2: continuous mode: Mimosa26 is readout continuously,
        data is send for each trigger, if multiple triggers exist data is
        duplicated and send several times. No trigger veto, the readout
        is active all the time. Asserting a unique trigger number has to
        be done in software using time reference devices.
'''

import argparse
import logging
import os
import time
import sys

import numpy as np
import tables as tb
from tqdm import tqdm
import yaml

import pymosa
from pymosa.m26 import M26

logger = logging.getLogger('EUDAQ Producer')


class EudaqScan(M26):
    scan_id = "eudaq_scan"

    last_readout_data = None
    last_trigger = 0

#     def __init__(self, conf=None):
#         super(EudaqScan, self).__init__(conf)
# 
#     def init(self, init_conf=None, configure_m26=False):
#         super(EudaqScan, self).init(init_conf, configure_m26)

#     def set_callback(self, fun):
#         ''' Set function to be called for each raw data chunk of one trigger '''
#         self.callback = fun
# 
#     def handle_data(self, data_tuple):
#         ''' Called on every readout (a few Hz)
# 
#             Sends data per event by checking for the trigger word that comes first.
#         '''
#         super(EudaqScan, self).handle_data(data_tuple)
#         raw_data = data_tuple[0]
# 
#         if np.any(self.last_readout_data):  # no last readout data for first readout
#             actual_data = np.concatenate((self.last_readout_data, raw_data))
#         else:
#             actual_data = raw_data
# 
#         trg_idx = np.where(actual_data & au.TRIGGER_ID > 0)[0]
#         trigger_data = np.split(actual_data, trg_idx)
# 
#         # Send data of each trigger
#         for dat in trigger_data[:-1]:
#             if np.any(dat):
#                 trigger = dat[0] & au.TRG_MASK
#                 if self.last_trigger > 0 and trigger != self.last_trigger + 1:
#                     logging.warning('Expected != Measured trigger number: %d != %d', self.last_trigger + 1, trigger)
#                 self.last_trigger = dat[0] & au.TRG_MASK
#                 self.callback(dat)
# 
#         self.last_readout_data = trigger_data[-1]
# 
#     def stop_readout(self, timeout=10.0):
#         super(EudaqScan, self).stop_readout(timeout)
#         # Send remaining data after stopped readout
#         self.callback(self.last_readout_data)


def replay_triggered_data(data_file, real_time=True):
    ''' Yield raw data for every trigger.

        real_time: boolean
            Delays return if replay is too fast to keep
            replay speed at original data taking speed.
    '''

    with tb.open_file(data_file, mode="r") as in_file_h5:
        meta_data = in_file_h5.root.meta_data[:]
        raw_data = in_file_h5.root.raw_data
        n_readouts = meta_data.shape[0]

        last_readout_time = time.time()

        # Leftover data from last readout
        last_readout_data = np.array([], dtype=np.uint32)
        last_trigger = -1

        for i in tqdm(range(n_readouts)):
            # Raw data indeces of readout
            i_start = meta_data['index_start'][i]
            i_stop = meta_data['index_stop'][i]

            t_start = meta_data[i]['timestamp_start']

            # Determine replay delays
            if i == 0:  # Initialize on first readout
                last_timestamp_start = t_start
            now = time.time()
            delay = now - last_readout_time
            additional_delay = t_start - last_timestamp_start - delay
            if real_time and additional_delay > 0:
                # Wait if send too fast, especially needed when readout was
                # stopped during data taking (e.g. for mask shifting)
                time.sleep(additional_delay)
            last_readout_time = time.time()
            last_timestamp_start = t_start

            actual_data = np.concatenate((last_readout_data, raw_data[i_start:i_stop]))
            trg_idx = np.where(actual_data & au.TRIGGER_ID > 0)[0]
            trigger_data = np.split(actual_data, trg_idx)

            # Special case: last readout, do not keep data for next readout
            if i == n_readouts - 1:
                trigger_data += [trigger_data[-1]]

            for dat in trigger_data[:-1]:
                if np.any(dat):
                    trigger = dat[0] & au.TRG_MASK
                    if last_trigger > 0 and trigger != last_trigger + 1:
                        logging.warning('Expected != Measured trigger number: %d != %d', last_trigger + 1, trigger)
                    last_trigger = dat[0] & au.TRG_MASK

                yield dat

            last_readout_data = trigger_data[-1]


def main():
    # Parse program arguments
    parser = argparse.ArgumentParser(prog='pymosa_eudaq',
                                     description="Start EUDAQ producer for pymosa",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('address', metavar='address',
                        help='Destination address',
                        default='tcp://localhost:44000',
                        nargs='?')
    parser.add_argument('--path', type=str,
                        help='Absolute path of your EUDAQ installation')
    parser.add_argument('--replay', type=str,
                        help='Raw data file to replay for testing')
    parser.add_argument('--delay', type=float,
                        help='Additional delay when replaying data in seconds')
    args = parser.parse_args()

    # Open Mimosa26 std. configuration
    pymosa_path = os.path.dirname(pymosa.__file__)
    #with open(os.path.join(pymosa_path, 'm26_configuration.yaml'), 'r') as f:
    with open('m26_configuration.yaml', 'r') as f:
        init_conf = yaml.load(f,Loader=yaml.FullLoader)

    # Create telescope object and load hardware configuration
    telescope = EudaqScan(conf=None)  # None: use default hardware configuration, m26.yaml

    # Import EUDAQ python wrapper with error handling
    try:
        from PyEUDAQWrapper import PyProducer
    except ImportError:
        if not args.path:
            logger.error('Cannot find PyEUDAQWrapper! Please specify the path of your EUDAQ installation!')
            return
        else:
            wrapper_path = os.path.join(args.path, 'python/')
            sys.path.append(os.path.join(args.path, 'python/'))
            try:
                from PyEUDAQWrapper import PyProducer
            except ImportError:
                logger.error('Cannot find PyEUDAQWrapper in %s', wrapper_path)
                return

    logger.info('Connect to %s', args.address)

    if args.replay:
        if os.path.isfile(args.replay):
            logger.info('Replay %s', args.replay)
        else:
            logger.error('Cannot open %s for replay!', args.replay)
    delay = args.delay if args.delay else 0.

    pp = PyProducer("pyBAR", args.address)  # FIXME: wrong type

    # Initialize telescope hardware and set up parameters
    ## telescope.init(init_conf=init_conf, configure_m26=False)

    # Start state mashine, keep connection until termination of euRun
    while not pp.Error and not pp.Terminating:
        # Wait for configure cmd from RunControl
        while not pp.Configuring and not pp.Terminating:
            if pp.StartingRun:
                break
            time.sleep(0.1)

        # Check if configuration received
        if pp.Configuring:
            logger.info('Configuring...')
            if not args.replay:
                try:
                    fix_filename = pp.GetConfigParameter("filename")
                except ValueError:  # Not defined
                    fix_filename = None
                ## telescope.configure_m26()
            else:
                time.sleep(3)
            pp.Configuring = True

        # Check for start of run cmd from RunControl
        while not pp.StartingRun and not pp.Terminating:
            if pp.Configuring:
                break
            time.sleep(0.1)

        # Check if we are starting:
        if pp.StartingRun:
            logger.info('Starting run...')

            if not args.replay:
                # Setup external trigge scan
                telescope.run_number = pp.GetRunNumber()
                if fix_filename:
                    telescope.output_filename = fix_filename
                else:
                    telescope.output_filename = 'pymosa_EUDAQ_RUN_%d' % pp.GetRunNumber()
                logging.info('Store raw data in file %s', telescope.output_filename)

                pp.StartingRun = True  # set status and send BORE
                # Start telescope readout
                ## telescope.start()
                # Run loop for normal data taking
                while True:
                    if pp.Error or pp.Terminating:
                        logger.info('Stopping run...')
                        telescope.stop_scan = True
                        break
                    if pp.StoppingRun:
                        logger.info('Stopping run...')
                        # FIXME: using not thread safe variable
                        telescope.stop_scan = True
                        break
                    time.sleep(0.1)
            else:  # Run loop to replay data
                pp.StartingRun = True  # set status and send BORE
                for raw_data in replay_triggered_data(data_file=args.replay):
                    pp.SendEvent(raw_data)
                    if pp.Error or pp.Terminating:
                        break
                    if pp.StoppingRun:
                        break
                    time.sleep(delay)

            # Abort conditions
            if pp.Error or pp.Terminating:
                pp.StoppingRun = False  # Set status and send EORE
            # Check if the run is stopping regularly
            if pp.StoppingRun:
                pp.StoppingRun = True  # Set status and send EORE

        # Back to check for configured + start run state
        time.sleep(0.1)


if __name__ == "__main__":
    # When run in development environment, eudaq path can be added with:
    sys.path.append('/home/davidlp/git/eudaq/python/')
    main()