# StudyRoomBot
This is a Discord Bot with the ability to query wolfram alpha and Google, and the same timer functionality as my StudyTimer: music + timer + reminder.

这个Discord bot能够访问wolfram alpha 和 google，并且在计时器方面与我的StudyTimer无异：音乐+计时器+提醒。

## Important
Need WolframLanguage Installed!
Also, edit the config.yml
IDs can be found in discord using developer mode
Wolfram那个东西也要下载
然后修改config.yml
那些ID可以在discord进入开发者模式后找到

## Usage
I tried to make it as simple as possible only to discover that more work then I expected is required. 

The program reads from a file call musicList.txt which remembers which song you are on last time. It reads the song from that musicList.txt, so if you changed the songs in the folder you should probably delete this file and have the program generate another one. Doing this allows me to implement a custom list or a random list in the future.

The songs are saved inside a folder called music, there is no deep first search which means that it only reads them in the folder, not subfolder. 

If they do not exists, the program creates them, other than that, there's not any code to prevent something from going wrong.

Commands To Bot
/help for all commands

Start Clock    => /start
Stopp Clock    => /stop
Print time     => /time
Remind         => /remindMeIn (minutes) (msg)
Play Music     => /music initialize
          play => /music play
         pause => /music pause
      get name => /music name
     next song => /music next
     last song => /music previous
Type Set math  => /typeSetMath (equation)
Search Wolfram => /wolfram (query)
Search Google  => /google (query)

尽管试着让他尽可能的简单好用，但我发现那样的话要写的东西实在是太多了。这个程序会从一个叫musicList.txt的地方读取所有的音乐列表以及应该从哪一首开始播放，因此，如果你修改了文件夹里的文件最好直接删掉让程序重新生成这个文件，而这个文件的用途就是以后可以自定义列表或者搞随机列表。所有的音乐都应该放在一个叫做music的文件夹下面，而且没有deep first search，所以只有在这个文件夹而非子文件夹中的文件才有效。这两个文件，如果不存在的话软件会自动生成，其余的保护机制都没有写。

对机器人发命令
/help查看全部

开始时钟 => /start
停止时钟 => /stop
印出时间 => /time
提醒 => /remindMeIn (minutes) (msg)
音乐启动 => /music initialize
播放 => /music play
暂停 => /music pause
歌名 => /music name
下一首 => /music next
最后一首 => /music previous
显示数学 => /typeSetMath (equation)
搜索wolfram => /wolfram (query)
搜索Google => /google (query)
   
By default there's only one song, feel free to add more to your own list.