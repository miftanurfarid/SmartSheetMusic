import pyaudio
import numpy as np
import os
import utils_audio_transcript as utils
import matplotlib.pyplot as plt
import time
import datetime as dt
import librosa as lb
import pandas as pd
from random import randint

class Position():
    
    min_data_sec = 3 # min volume of data needed before processing (in secs)
    data_buffer_sec = 4 # volume of data we keep to do the comparison
    proba_redistribution_fract = 0.2 # the fraction of probability mass that goes to unlikely positions
        
    def __init__(self, wd, filename, samplerate, win_s, hop_s, verbose =  False, plot_online = False):   
        
        # Fixed parameters
        self.sample_rate_est = samplerate         
        self.win_s_est = win_s # win_s used for processing
        self.hop_s_est = hop_s # hop_s used for processing
        self.verbose = verbose
        
        # Initialise profiling report
        self.profiling_report = ''             
        
        # Load the spectrogram from the disk and turn into chromagram            
        spec_data_act = utils.load_spectrogram_from_disk(wd, filename, samplerate, win_s, hop_s)
        self.chromagram_act = utils.spectrogram_to_chromagram(spec_data_act["spectrogram"], spec_data_act["frequencies"])
        self.sample_rate_act = spec_data_act["samplerate"]
        self.win_s_act = spec_data_act["win_s"]
        self.hop_s_act = spec_data_act["hop_s"]
        self.len_chromagram_sec = self.chromagram_act.shape[0] * self.hop_s_act / float(self.sample_rate_act) 
        
        # Initialise audio_data to an empty list
        self.audio_data = [] 
        
        # Initialise the total recording time to 0
        self.total_recording_sec = 0.0
        
        # Initialise positions probabilities with equi-probabilities
        probas = np.ones(spec_data_act["spectrogram"].shape[0]) / float(spec_data_act["spectrogram"].shape[0])
        time_sec = np.arange(spec_data_act["spectrogram"].shape[0]) * spec_data_act["hop_s"] / float(spec_data_act["samplerate"]) 
        self.position_proba = utils.relative_ts_to_absolute_ts(time_sec, probas)  
        
        # Calibrate the noise level
        self.calibrate_noise_level()
        
        # Axis placeholder for plotting
        if plot_online:
            plt.ion()
            _, gca = plt.subplots()
            self.gca = gca
        
    def calibrate_noise_level(self):
        
        start_time = time.time()
        
        # Find how many frames correspond to the time buffer
        nb_frames = (self.data_buffer_sec * self.sample_rate_act) // self.hop_s_act
        
        nb_frames_tot = self.chromagram_act.shape[0]
        idxs1 = range(nb_frames_tot-nb_frames)
        
        # Find the distance that corresponds to noise
        # i.e. what is the distance when we compare two non-overlapping samples?
        nb_boot = 1000
        dists = np.zeros(nb_boot)
        for i in range(nb_boot):
            idx_start1 = randint(0, len(idxs1)-1)
            idxs2 = idxs1[:idx_start1-nb_frames] + idxs1[idx_start1+nb_frames:]
            idx_start2 = idxs2[randint(0, len(idxs2)-1)]
            chroma1 = self.chromagram_act[idx_start1:(idx_start1+nb_frames),]
            chroma2 = self.chromagram_act[idx_start2:(idx_start2+nb_frames),] 
            dists[i] = utils.distance_chroma(chroma1,chroma2, chroma1.size)
        
        self.noise_level = np.percentile(dists, 5.0)
        
        self.profiling_report += "%0.4fs seconds for noise calibration\r" % (time.time()-start_time)
    
    def plot_online(self):
        
        utils.plot_chromagram(self.chromagram_est, self.sample_rate_est, self.hop_s_est, self.gca)
        plt.pause(0.05)
        
        
        
    def update_audio_data(self, new_audio_data):
        
        # Make sure the mod(chunk size, hop size) == 0
        # Otherwise, this will cause troubles later (e.g. when shifting probas)
        len_new_audio_data = len(new_audio_data)
        if (len_new_audio_data % self.hop_s_act) != 0: raise ValueError('Mod(chunk size, hop size) != 0')
        
        # Add the new data
        self.audio_data.extend(new_audio_data)        
        
        # If there is more data than what we need, then remove the leftmost items
        self.len_audio_data = len(self.audio_data)        
        if  self.len_audio_data > self.sample_rate_est * Position.data_buffer_sec:
            # Make sure that mod(len_audio_data, hop_s) == 0
            nb_items_to_keep = ((self.sample_rate_est * Position.data_buffer_sec) // self.hop_s_est)*self.hop_s_est 
            self.len_audio_data = nb_items_to_keep
            len_tmp = len(self.audio_data)
            del self.audio_data[:(len_tmp-nb_items_to_keep)]        
        
        # Keep a counter (in secs) for the length of the audio data
        self.len_audio_data_sec = self.len_audio_data / float(self.sample_rate_est)
        
        # Time difference (in secs) between previous and current chunk of audio data
        self.timedelta = len_new_audio_data / float(self.sample_rate_est)
        
        # Keep track of the total recording time
        self.total_recording_sec += self.timedelta             
     
    def distance_to_proba(self, ts_dist, conversion_type = 'inverse_denoised'):
        
        if conversion_type == 'inverse':            
            ts_proba = 1.0 / ts_dist        
        elif conversion_type == 'negative':
            ts_proba = -ts_dist
        elif conversion_type == 'softmax_negative':
            ts_proba = np.exp(-ts_dist)
        elif conversion_type == 'inverse_denoised':
            idxs_noise = ts_dist.index[ts_dist > self.noise_level]   
            ts_proba = 1.0 / ts_dist
            ts_proba[idxs_noise] = 0.0
            
        # Normalise to get a well-defined probability measure
        ts_proba = ts_proba / np.sum(ts_proba)
        
#         ordered_position_proba = ts_proba.order(ascending = False)
#         print ordered_position_proba[0:5] 
        
        # Restribute a bit of the probabilities mass towards unlikely positions 
        # (this should break if conversion_type != 'inverse_denoised')        
        ts_proba[ts_proba.index.difference(idxs_noise)] = (1.0 - Position.proba_redistribution_fract) * ts_proba[ts_proba.index.difference(idxs_noise)].values
        ts_proba[idxs_noise] = Position.proba_redistribution_fract * np.ones(len(idxs_noise)) / float(len(idxs_noise))   
        
        self.position_proba_new = ts_proba
        
    def update_position_proba(self):
        
        # Start by shifting the prior by the size of the new data 
        # has has been received since last update 
        position_proba_old = self.position_proba.shift(1, freq = dt.timedelta(seconds = self.timedelta))
        
        # Build the search range and filter the two proba times series        
        min_search = dt.timedelta(seconds=self.total_recording_sec) + utils.START_DATETIME 
        max_search = dt.timedelta(seconds=self.len_chromagram_sec-self.len_audio_data_sec) + utils.START_DATETIME 
        position_proba_old = position_proba_old[np.logical_and(position_proba_old.index >= min_search, position_proba_old.index <= max_search)]
        position_proba_new = self.position_proba_new[np.logical_and(self.position_proba_new.index >= min_search, self.position_proba_new.index <= max_search)]
        
        # Fit the new probas to the old probas 
        # (the timedelta in the timestamps is non-zero only because of numerical errors)   
        position_proba_new = position_proba_new.asof(position_proba_old.index)
        
        # Multiply the prior probability and current measurement 
        # to get the new probability
        position_proba_updated = position_proba_old.multiply(position_proba_new)
        
        # Renormalise
        self.position_proba = position_proba_updated / np.sum(position_proba_updated) 
                                
            
    def find_position(self, new_audio_data): 
        
        # Start by adding the new audio data to the existing data        
        Position.update_audio_data(self, new_audio_data)
        
        # Check if we have enough data to run the position algorithm
        if self.len_audio_data_sec > Position.min_data_sec:
            # 1. Get the spectrogram for the estimated data
            # (may be not ideal to re-estimate the full spectrogram for each update)
            start_time = time.time()                        
            spec_data_est = utils.get_audio_data_spectrogram(self.audio_data, self.sample_rate_est, self.win_s_est, self.hop_s_est)
            spectrogram_time = time.time() - start_time            
            
            # 2. Convert spectrogram to chromagram
            start_time = time.time()
            self.chromagram_est = utils.spectrogram_to_chromagram(spec_data_est["spectrogram"], spec_data_est["frequencies"])
            chromagram_time = time.time() - start_time
            
            # 3. Find the distance between the two chromagrams
            start_time = time.time()
            ts_dist = utils.compare_chomagrams(self.chromagram_act, self.chromagram_est, self.sample_rate_est, self.hop_s_est)
            comparison_time = time.time() - start_time
            
            # 4. Turn the distance into an (unconditional) probability
            Position.distance_to_proba(self, ts_dist)
            
            # 5. Use new and previous information to update the probability
            start_time = time.time()
            Position.update_position_proba(self)
            proba_time = time.time() - start_time
            
            # Add the timing informations to the profiling report
            self.profiling_report += "%0.4fs seconds for spectrogram\r" % spectrogram_time
            self.profiling_report += "%0.4fs seconds for chromagram\r" % chromagram_time 
            self.profiling_report += "%0.4fs seconds for comparison\r" % comparison_time 
            self.profiling_report += "%0.4fs seconds for proba update\r" % proba_time  
                
            ordered_position_proba = self.position_proba.order(ascending = False)
            print ordered_position_proba[0:5] #ts_dist.idxmin()
            
#             Position.plot_chromagrame_est_online(self)


def callback(in_data, frame_count, time_info, flag):
    
    global pos
    
    audio_data = np.fromstring(in_data, dtype=np.float32).tolist()
    pos.find_position(audio_data)    
    
    return(('', False))

wd = "C:/Users/Alexis/Desktop/Business/SmartSheetMusic/Samples/Nocturnes/"
filename = "nocturnes" 
filename_wav = wd + filename + ".wav"
filename_midi = wd + filename + ".mid"

CHUNK = 16384

SAMPLERATE = 11025
WIN_S = 4096
HOP_S = 2048
# utils.write_spectrogram_to_disk(wd, filename, SAMPLERATE, WIN_S, HOP_S)

pos = Position(wd, filename, SAMPLERATE, WIN_S, HOP_S, verbose = False)

mode = 'online'

if mode == 'offline':
    spec_data = utils.get_wav_spectrogram(filename_wav, samplerate = SAMPLERATE, win_s = WIN_S, hop_s = HOP_S, start_sec = 217.0, end_sec = 225.0)
    chromagram = utils.spectrogram_to_chromagram(spec_data["spectrogram"], spec_data["frequencies"])
    fig = utils.plot_chromagram(chromagram, SAMPLERATE, HOP_S)
    a=0
#     s = lb.core.load(filename_wav, sr = SAMPLERATE, offset = 35, duration = 20)
#     for i in range(50):
#         samples = s[0][i*CHUNK:(i+1)*CHUNK]
#         pos.find_position(samples)
elif mode == 'online':
# os.system("start " + filename_wav)
    p = pyaudio.PyAudio()
     
    stream = p.open(format = pyaudio.paFloat32,
                    channels = 1,
                    rate = SAMPLERATE,
                    input = True,
                    frames_per_buffer = CHUNK, 
                    stream_callback = callback)
     
    print("* recording")
     
    stream.start_stream()
     
    while stream.is_active():
        time.sleep(0.1)
    print("* done recording")
     
    stream.stop_stream()
    stream.close()
    p.terminate()

