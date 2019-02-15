import numpy as np
import matplotlib.mlab as mlab
import matplotlib.pyplot as plt
from scipy.ndimage.filters import maximum_filter
from scipy.ndimage.morphology import (generate_binary_structure,
                                      iterate_structure, binary_erosion)
import hashlib
from operator import itemgetter

IDX_FREQ_I = 0
IDX_TIME_J = 1

######################################################################
# Sampling rate, related to the Nyquist conditions, which affects
# the range frequencies we can detect.
DEFAULT_FS = 44100

######################################################################
# Size of the FFT window, affects frequency granularity
DEFAULT_WINDOW_SIZE = 4096

######################################################################
# Ratio by which each sequential window overlaps the last and the
# next window. Higher overlap will allow a higher granularity of offset
# matching, but potentially more fingerprints.
DEFAULT_OVERLAP_RATIO = 0.5

######################################################################
# Degree to which a fingerprint can be paired with its neighbors --
# higher will cause more fingerprints, but potentially better accuracy.
DEFAULT_FAN_VALUE = 15

######################################################################
# Minimum amplitude in spectrogram in order to be considered a peak.
# This can be raised to reduce number of fingerprints, but can negatively
# affect accuracy.
DEFAULT_AMP_MIN = 10

######################################################################
# Number of cells around an amplitude peak in the spectrogram in order
# for Dejavu to consider it a spectral peak. Higher values mean less
# fingerprints and faster matching, but can potentially affect accuracy.
PEAK_NEIGHBORHOOD_SIZE = 20

######################################################################
# Thresholds on how close or far fingerprints can be in time in order
# to be paired as a fingerprint. If your max is too low, higher values of
# DEFAULT_FAN_VALUE may not perform as expected.
MIN_HASH_TIME_DELTA = 0
MAX_HASH_TIME_DELTA = 200

######################################################################
# If True, will sort peaks temporally for fingerprinting;
# not sorting will cut down number of fingerprints, but potentially
# affect performance.
PEAK_SORT = True

######################################################################
# Number of bits to throw away from the front of the SHA1 hash in the
# fingerprint calculation. The more you throw away, the less storage, but
# potentially higher collisions and misclassifications when identifying songs.
FINGERPRINT_REDUCTION = 20

def fingerprint(channel_samples, Fs=DEFAULT_FS,
                wsize=DEFAULT_WINDOW_SIZE,
                wratio=DEFAULT_OVERLAP_RATIO,
                fan_value=DEFAULT_FAN_VALUE,
                amp_min=DEFAULT_AMP_MIN):
    '''对每一个声道分段编码
        Args:
            channel_samples: 每个声道的数据，在decoder.py文件中生成的
            Fs: 音频的采样频率
    '''

    """
    FFT the channel, log transform output, find local maxima, then return
    locally sensitive hashes.
    """
    # FFT the signal and extract frequency components
    # 绘制频谱图
    arr2D = mlab.specgram( 
        channel_samples,
        NFFT=wsize,
        Fs=Fs,
        window=mlab.window_hanning,
        noverlap=int(wsize * wratio))[0] # 块之间重叠点的数目。

    # apply log transform since specgram() returns linear array
    arr2D = 10 * np.log10(arr2D) # np.log10 计算以10为底的对数
    arr2D[arr2D == -np.inf] = 0  # replace infs with zeros,-np.inf正负无穷大的数

    # find local maxima
    local_maxima = get_2D_peaks(arr2D, plot=False, amp_min=amp_min)

    # return hashes
    return generate_hashes(local_maxima, fan_value=fan_value)


def get_2D_peaks(arr2D, plot=False, amp_min=DEFAULT_AMP_MIN):
    '''将频谱图简化成只有峰值的图'''
    # https://docs.scipy.org/doc/scipy/reference/generated/scipy.ndimage.generate_binary_structure.html
    struct = generate_binary_structure(2, 1) # 生成2维框架
    neighborhood = iterate_structure(struct, PEAK_NEIGHBORHOOD_SIZE)

    # find local maxima using our fliter shape
    local_max = maximum_filter(arr2D, footprint=neighborhood) == arr2D
    background = (arr2D == 0)
    eroded_background = binary_erosion(background, structure=neighborhood,
                                       border_value=1)

    # Boolean mask of arr2D with True at peaks
    detected_peaks = local_max ^ eroded_background

    # extract peaks
    amps = arr2D[detected_peaks]
    j, i = np.where(detected_peaks)

    # filter peaks
    amps = amps.flatten()
    peaks = zip(i, j, amps)
    peaks_filtered = [x for x in peaks if x[2] > amp_min]  # freq, time, amp

    # get indices for frequency and time
    frequency_idx = [x[1] for x in peaks_filtered]
    time_idx = [x[0] for x in peaks_filtered]

    if plot:
        # scatter of the peaks
        fig, ax = plt.subplots()
        ax.imshow(arr2D)
        ax.scatter(time_idx, frequency_idx)
        ax.set_xlabel('Time')
        ax.set_ylabel('Frequency')
        ax.set_title("Spectrogram")
        plt.gca().invert_yaxis()
        plt.show()

    return zip(frequency_idx, time_idx) # 峰值即峰值对应的时间点


def generate_hashes(peaks, fan_value=DEFAULT_FAN_VALUE):
    '''将get_2D_peaks生成的峰值、时间序列，生成hash值
        Args:
            peaks:
            fan_value: target zone的大小
        Returns:
            h.hexdigest()[0:FINGERPRINT_REDUCTION]: 峰值1、峰值2的频率和两峰值之间的时间差的sha1哈希，并只取0-20位
            t1: 峰值1的时间，也就是从音频开始到峰值1之间的时间差  
    '''
    
    """
    Hash list structure:
       sha1_hash[0:20]    time_offset
    [(e05b341a9b77a51fd26, 32), ... ]
    """

    peaks = list(peaks)
    peaks.sort()
    #if PEAK_SORT:
    #    peaks.sort(key=itemgetter(1))

    for i in range(len(peaks)): # 遍历所有的峰值
        for j in range(1, fan_value): # 每个峰值都要与target zone中的峰值两两结合
            if (i + j) < len(peaks):
                
                freq1 = peaks[i][IDX_FREQ_I] # 峰值1对应的频率
                freq2 = peaks[i + j][IDX_FREQ_I] # 峰值2对应的频率
                t1 = peaks[i][IDX_TIME_J] # 峰值1对应的时间
                t2 = peaks[i + j][IDX_TIME_J] # 峰值2对应的时间
                t_delta = t2 - t1 # 两个峰值之间的时间差offset

                maybe_hash = "%s|%s|%s" % (str(freq1), str(freq2), str(t_delta)) # 将峰值1、峰值2和两个峰值之间的时间差组成一个hash值

                if t_delta >= MIN_HASH_TIME_DELTA and t_delta <= MAX_HASH_TIME_DELTA:
                    h = hashlib.sha1()
                    h.update(maybe_hash.encode('utf-8'))
                    # 只保留maybe_hash的0-20，为了节省时间和空间
                    yield (h.hexdigest()[0:FINGERPRINT_REDUCTION], t1) # 将hash值与峰值1对应的时间作为一条记录
