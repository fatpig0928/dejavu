import os
import fnmatch
import numpy as np
from pydub import AudioSegment
from pydub.utils import audioop
import wavio
from hashlib import sha1

def unique_hash(filepath, blocksize=2**20):
    '''使用sha1编码音频，获得音频的唯一编码，用来标识一个音频
        Args:
            filepath: 音频的绝对路径
            blocksize: 从文件中读取的字节数
        Returns:
            s.hexdigest().upper(): 编码后并将小写转为大写的音频
    '''
    
    """ Small function to generate a hash to uniquely generate
    a file. Inspired by MD5 version here:
    http://stackoverflow.com/a/1131255/712997

    Works with large files. 
    """
    s = sha1()
    with open(filepath , "rb") as f:
        while True:
            buf = f.read(blocksize) # blocksize从文件中读取的字节数
            if not buf:
                break
            s.update(buf) # 分块多次调用和一次调用是一样的
    return s.hexdigest().upper() # upper将字符串中的小写字母转为大写字母


def find_files(path, extensions):
    '''生成每个音频的绝对路径的path和后缀名
        Args: 
            path: 存放音频的文件夹
            extensions: 可生成指纹的音频后缀名
        Returns:
            p: 音频的绝对路径
            extension: 该音频的后缀名
    '''
    # Allow both with ".mp3" and without "mp3" to be used for extensions
    extensions = [e.replace(".", "") for e in extensions] # 将'.mp3'中的点替换为''

    for dirpath, dirnames, files in os.walk(path):
        for extension in extensions:
            for f in fnmatch.filter(files, "*.%s" % extension): # 通过多个可能的文件扩展名来过滤文件
                p = os.path.join(dirpath, f)
                yield (p, extension) # 返回一个生成器，图片的绝对路径和后缀名


def read(filename, limit=None):
    '''读取音频文件，获得音频的采样频率和编码
        Args:
            filename: 音频的name(相对路径)
            limit: 限制一个音频编码几分钟，从开始数几秒钟
        Returns:
            channels: list，每个元素是一个声道的数据
            audiofile.frame_rate: 音频的采样频率
            unique_hash(filename): 该音频的sha1编码
    '''

    """
    Reads any file supported by pydub (ffmpeg) and returns the data contained
    within. If file reading fails due to input being a 24-bit wav file,
    wavio is used as a backup.

    Can be optionally limited to a certain amount of seconds from the start
    of the file by specifying the `limit` parameter. This is the amount of
    seconds from the start of the file.

    returns: (channels, samplerate)
    """
    # pydub does not support 24-bit wav files, use wavio when this occurs
    try:
        audiofile = AudioSegment.from_file(filename)

        if limit:
            audiofile = audiofile[:limit * 1000]

        data = np.fromstring(audiofile._data, np.int16) # 使用字符串创建array

        channels = []
        for chn in range(audiofile.channels): # 音频的声道数
            channels.append(data[chn::audiofile.channels])

        fs = audiofile.frame_rate # 音频的采样频率
    except audioop.error: # 如果使用AudioSegment有错误，就是用wavio
        fs, _, audiofile = wavio.readwav(filename)

        if limit:
            audiofile = audiofile[:limit * 1000]

        audiofile = audiofile.T
        audiofile = audiofile.astype(np.int16)

        channels = []
        for chn in audiofile:
            channels.append(chn)

    return channels, audiofile.frame_rate, unique_hash(filename)


def path_to_songname(path):
    '''提取path中的文件名，不含后缀
        Args: 
            path: 类似'mp3/Brad-Sucks--Total-Breakdown.mp3'
        Returns:
            os.path.splitext(os.path.basename(path))[0]: Brad-Sucks--Total-Breakdown
    '''

    """
    Extracts song name from a filepath. Used to identify which songs
    have already been fingerprinted on disk.
    """
    return os.path.splitext(os.path.basename(path))[0] 
