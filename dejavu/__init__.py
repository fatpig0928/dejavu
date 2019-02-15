from dejavu.database import get_database, Database
import dejavu.decoder as decoder
from dejavu.fingerprint import *
import multiprocessing
import os
import traceback
import sys


class Dejavu(object):

    SONG_ID = "song_id"
    SONG_NAME = 'song_name'
    CONFIDENCE = 'confidence'
    MATCH_TIME = 'match_time'
    OFFSET = 'offset'
    OFFSET_SECS = 'offset_seconds'

    def __init__(self, config):
        super(Dejavu, self).__init__()

        self.config = config

        # initialize db
        db_cls = get_database(config.get("database_type", None))

        self.db = db_cls(**config.get("database", {}))
        self.db.setup()

        # if we should limit seconds fingerprinted,
        # None|-1 means use entire track
        # 限制fingerprint的秒数
        self.limit = self.config.get("fingerprint_limit", None) # dict的get方法，返回指定键的值，如果值不在字典中，则返回第二个参数default的值
        if self.limit == -1:  # for JSON compatibility
            self.limit = None
        self.get_fingerprinted_songs()

    def get_fingerprinted_songs(self):
        # get songs previously indexed
        self.songs = self.db.get_songs() # 得到song表中的所有已经生成指纹的歌曲的id，name，和sha1编码
        self.songhashes_set = set()  # to know which ones we've computed before
        for song in self.songs:
            song_hash = song[Database.FIELD_FILE_SHA1]
            self.songhashes_set.add(song_hash) # self.songhashes_set是所有歌曲的sha1编码

    def fingerprint_directory(self, path, extensions, nprocesses=None):
        '''对音频进行编码，并存到数据库中，每个音频先分成声道，再对每个声道按照一节一节的编码
            Args:
                path: 音频的路径
                extensions: 扩展名
                nprocesses: 多进程的数量
        '''
        # Try to use the maximum amount of processes if not given.
        try:
            nprocesses = nprocesses or multiprocessing.cpu_count() # 返回当前系统有多少个cpu
        except NotImplementedError:
            nprocesses = 1
        else:
            nprocesses = 1 if nprocesses <= 0 else nprocesses

        pool = multiprocessing.Pool(nprocesses) # 进程池

        filenames_to_fingerprint = [] # 需要进行fingerprint的音频路径
        for filename, _ in decoder.find_files(path, extensions):

            # don't refingerprint already fingerprinted files
            if decoder.unique_hash(filename) in self.songhashes_set: # 对当前音频进行sha1编码，然后和所有歌曲的sha1编码list对比，有则不重复编码
                print("%s already fingerprinted, continuing..." % filename)
                continue

            filenames_to_fingerprint.append(filename)

        # Prepare _fingerprint_worker input
        # worker_input为要进行fingerprint的zip元组
        worker_input = zip(filenames_to_fingerprint,
                           [self.limit] * len(filenames_to_fingerprint)) # [None]*2==[None, None]


        # Send off our tasks
        # 每个多进程处理一首歌，进程数与cpu数相同，比如有5首歌，开了4个进程，这个4个进程处理完4首歌后，再从进程池里拿一个进程处理最后一首歌
        iterator = pool.imap_unordered(_fingerprint_worker,
                                       worker_input) # imap_unordered和map类似，第一个参数是函数，第二个参数是迭代器，将迭代器中的数放到函数里执行

        # Loop till we have all of them
        while True:
            try:
                # _fingerprint_worker()对每个音频编码的结果
                song_name, hashes, file_hash = iterator.next()
            except multiprocessing.TimeoutError:
                continue
            except StopIteration:
                break
            except:
                print("Failed fingerprinting")
                # Print traceback because we can't reraise it here
                traceback.print_exc(file=sys.stdout)
            else:
                # 这个地方就可以把song_name和file_hash和hashes存到文本给洪宁，不过还得看一下，他是怎么听歌识曲的
                sid = self.db.insert_song(song_name, file_hash) # 将音频name和使用sha1对音频编码存到song表里

                self.db.insert_hashes(sid, hashes) # 将插入song表中对应的id和hashes存到fingerprint表中，这样sid就是外键
                self.db.set_song_fingerprinted(sid) # 将song表中用于标志一首歌的指纹被存到数据库中的属性，改为1
                self.get_fingerprinted_songs()

        pool.close()
        pool.join()

    def fingerprint_file(self, filepath, song_name=None):
        songname = decoder.path_to_songname(filepath)
        song_hash = decoder.unique_hash(filepath)
        song_name = song_name or songname
        # don't refingerprint already fingerprinted files
        if song_hash in self.songhashes_set:
            print("%s already fingerprinted, continuing..." % song_name)
        else:
            song_name, hashes, file_hash = _fingerprint_worker(
                filepath,
                self.limit,
                song_name=song_name
            )
            sid = self.db.insert_song(song_name, file_hash)

            self.db.insert_hashes(sid, hashes)
            self.db.set_song_fingerprinted(sid)
            self.get_fingerprinted_songs()

    def find_matches(self, samples, Fs=DEFAULT_FS):
        '''查找结果
            Args: 
                samples: 音频的一个声道的数据
                Fs: 该音频的采样频率
            Returns:
                self.db.return_matches(hashes): 从数据库里查找到的所有的结果，sid和时间段
        '''
        hashes = fingerprint(samples, Fs=Fs) # samples是音频一个声道的数据，对该数据进行fingerprint
        return self.db.return_matches(hashes)

    def align_matches(self, matches):
        '''将数据库里查找到的所有结果进行加分，返回confidence最大的结果
            Args: 
                matches: 从数据库里查找到的所有的结果，(sid, db_offset - song_sampled_offset)
            Returns:
                song: dict {'song_id': 6, 'song_name': 'Choc--Eigenvalue-Subspace-Decomposition', 
                            'confidence': 6599, 'offset': 0, 'offset_seconds': 0.0, 
                            'file_sha1': '588419D6AF2127F6509BB67D53604B43BA83C581'}
        '''

        """
            Finds hash matches that align in time with other matches and finds
            consensus about which hashes are "true" signal from the audio.

            Returns a dictionary with match information.
        """
        # align by diffs
        diff_counter = {}
        largest = 0 # 最大confidence对应的diff
        largest_count = 0 # 最大的confidence
        song_id = -1 # 歌曲的id
        for tup in matches:
            sid, diff = tup 
            if diff not in diff_counter:
                diff_counter[diff] = {}
            if sid not in diff_counter[diff]:
                diff_counter[diff][sid] = 0
            diff_counter[diff][sid] += 1 # 以这种方式是因为，样本音频和目标音频相同hash的offset差，最多

            if diff_counter[diff][sid] > largest_count:
                largest = diff
                largest_count = diff_counter[diff][sid]
                song_id = sid
        print(diff_counter)

        # extract idenfication
        song = self.db.get_song_by_id(song_id) # 通过song_id获得songname和song表中的sha1码
        if song:
            # TODO: Clarify what `get_song_by_id` should return.
            songname = song.get(Dejavu.SONG_NAME, None)
        else:
            return None

        # return match info
        nseconds = round(float(largest) / DEFAULT_FS *
                         DEFAULT_WINDOW_SIZE *
                         DEFAULT_OVERLAP_RATIO, 5)
        song = {
            Dejavu.SONG_ID : song_id,
            Dejavu.SONG_NAME : songname,
            Dejavu.CONFIDENCE : largest_count,
            Dejavu.OFFSET : int(largest), # db_offset - song_sampled_offset
            Dejavu.OFFSET_SECS : nseconds, # offset的秒数，被识别音频找到数据库中的音频的第几秒种
            Database.FIELD_FILE_SHA1 : song.get(Database.FIELD_FILE_SHA1, None),}
        return song

    def recognize(self, recognizer, *options, **kwoptions):
        r = recognizer(self)
        return r.recognize(*options, **kwoptions)


def _fingerprint_worker(filename, limit=None, song_name=None):
    '''对音频进行编码的总函数
        Args:
            filename: 将要fingerprint的音频(路劲/xxx.mp3)
            limit: 限制每个音频编码多少秒
            song_name: 音频的name
        Returns:
            song_name: 音频的name(不含后缀名)
            result: 整个音频使用函数fingerprint()编码的结果，set()
            file_hash: 整个音频使用sha1编码的结果
    '''
    # Pool.imap sends arguments as tuples so we have to unpack
    # them ourself.
    try:
        filename, limit = filename
    except ValueError:
        pass

    songname, extension = os.path.splitext(os.path.basename(filename)) # 分离歌名和后缀名
    song_name = song_name or songname # or 找到第一个非空/逻辑非的对象
    # channels: list，每个元素是一个声道的数据
    # Fs: 音频的采样频率
    # file_hash: 该音频的sha1编码
    channels, Fs, file_hash = decoder.read(filename, limit)
    result = set()
    channel_amount = len(channels) # 音频的声道数

    for channeln, channel in enumerate(channels):
        # TODO: Remove prints or change them into optional logging.
        print("Fingerprinting channel %d/%d for %s" % (channeln + 1,
                                                       channel_amount,
                                                       filename))
        hashes = fingerprint(channel, Fs=Fs) # 对音频进行编码，但是file_hash不是已经用sha1编码完成了吗
        print("Finished channel %d/%d for %s" % (channeln + 1, channel_amount,
                                                 filename))
        result |= set(hashes) # |= 类似与list的append

    return song_name, result, file_hash


def chunkify(lst, n):
    '''把一个list分成n个几乎等长的部分'''
    """
    Splits a list into roughly n equal parts. 
    http://stackoverflow.com/questions/2130016/splitting-a-list-of-arbitrary-size-into-only-roughly-n-equal-parts
    """
    return [lst[i::n] for i in range(n)] # list切片，开始是i， 步长是n
