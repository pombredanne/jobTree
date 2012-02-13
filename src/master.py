#!/usr/bin/env python

#Copyright (C) 2011 by Benedict Paten (benedictpaten@gmail.com)
#
#Permission is hereby granted, free of charge, to any person obtaining a copy
#of this software and associated documentation files (the "Software"), to deal
#in the Software without restriction, including without limitation the rights
#to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#copies of the Software, and to permit persons to whom the Software is
#furnished to do so, subject to the following conditions:
#
#The above copyright notice and this permission notice shall be included in
#all copies or substantial portions of the Software.
#
#THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
#THE SOFTWARE.

"""The master component (of a master slave pattern) for a job manager used by 
sontrace programs (cactus etc) for running hierarchical trees of jobs on the 
cluster.

Takes a crash-only philosophy so that any part of the process can be failed 
and then restarted at will (see the accompanying tests).
"""

import os
import sys
import os.path 
import xml.etree.ElementTree as ET
import time
#from bz2 import BZ2File

from sonLib.bioio import logger, getTotalCpuTime
from sonLib.bioio import getLogLevelString
from sonLib.bioio import logFile
from sonLib.bioio import system
from jobTree.src.bioio import workflowRootPath
from threading import Thread, Lock
from Queue import Queue, Empty

def createJob(attrib, parent, config):
    """Creates an XML record for the job in a file within the hierarchy of jobs.
    """
    job = ET.Element("job")
    job.attrib["file"] = config.attrib["job_file_dir"].getTempFile(".xml")
    job.attrib["remaining_retry_count"] = config.attrib["retry_count"]
    job.attrib["colour"] = "grey"
    followOns = ET.SubElement(job, "followOns")
    ET.SubElement(followOns, "followOn", attrib.copy())
    if parent != None:
        job.attrib["parent"] = parent
    job.attrib["child_count"] = str(0)
    job.attrib["black_child_count"] = str(0)
    job.attrib["log_level"] = getLogLevelString()
    job.attrib["log_file"] = config.attrib["log_file_dir"].getTempFile(".log") #The log file for the actual command
    job.attrib["slave_log_file"] = config.attrib["slave_log_file_dir"].getTempFile(".log") #The log file for the slave
    job.attrib["global_temp_dir"] = config.attrib["temp_dir_dir"].getTempDirectory()
    job.attrib["job_creation_time"] = str(time.time())
    job.attrib["environment_file"] = config.attrib["environment_file"]
    job.attrib["job_time"] = config.attrib["job_time"]
    job.attrib["max_log_file_size"] = config.attrib["max_log_file_size"]
    job.attrib["default_memory"] = config.attrib["default_memory"]
    job.attrib["default_cpu"] = config.attrib["default_cpu"]
    if bool(int(config.attrib["reportAllJobLogFiles"])):
        job.attrib["reportAllJobLogFiles"] = ""
    if config.attrib.has_key("stats"):
        job.attrib["stats"] = config.attrib["log_file_dir"].getTempFile(".xml") #The file to store stats in..
    ET.SubElement(job, "children") 
    return job

def deleteJob(job, config):
    """Removes an old job, including any log files.
    """
    config.attrib["log_file_dir"].destroyTempFile(job.attrib["log_file"]) #These files must exist through out for the temp file tree to survive
    config.attrib["slave_log_file_dir"].destroyTempFile(job.attrib["slave_log_file"])
    config.attrib["temp_dir_dir"].destroyTempDir(job.attrib["global_temp_dir"])
    config.attrib["job_file_dir"].destroyTempFile(job.attrib["file"])
    if config.attrib.has_key("stats"):
        config.attrib["log_file_dir"].destroyTempFile(job.attrib["stats"])
        
def writeJob(job, jobFileName):
    tree = ET.ElementTree(job)
    fileHandle = open(jobFileName, 'w') 
    #fileHandle = BZ2File(jobFileName, 'w', compresslevel=5)
    tree.write(fileHandle)
    fileHandle.close()

def readJob(jobFile):
    logger.debug("Going to load the file %s" % jobFile)
    return ET.parse(jobFile).getroot()
    #fileHandle = open(jobFile, 'r')
    #fileHandle = BZ2File(jobFile, 'r')
    #job = ET.parse(fileHandle).getroot()
    #fileHandle.close()
    #return job

def writeJobs(jobs):
    """Writes a list of jobs to file, ensuring that the previous
    state is maintained until after the write is complete
    """
    if len(jobs) == 0:
        return
    assert len(set(jobs)) == len(jobs)
    #Create a unique updating name using the first file in the list
    fileName = jobs[0].attrib["file"]
    updatingFile = fileName + ".updating"
    
    #The existence of the the updating file signals we are in the process of creating an update to the state of the files
    assert not os.path.isfile(updatingFile)
    fileHandle = open(updatingFile, 'w')
    fileHandle.write(" ".join([ job.attrib["file"] + ".new" for job in jobs ]))
    fileHandle.close()
    
    #Update the current files.
    for job in jobs:
        newFileName = job.attrib["file"] + ".new"
        assert not os.path.isfile(newFileName)
        writeJob(job, newFileName)
       
    os.remove(updatingFile) #Remove the updating file, now the new files represent the valid state
    
    for job in jobs:
        if os.path.isfile(job.attrib["file"]):
            os.remove(job.attrib["file"])
        os.rename(job.attrib["file"] + ".new", job.attrib["file"])

def jobBatcherWorker(batchSystem, maxCpus, queue, lock, jobIDsToJobsHash, cpuQueue):
    """Function submits jobs to the batch system
    """
    usedCpus = 0
    jobTreeSlavePath = os.path.join(workflowRootPath(), "bin", "jobTreeSlave")
    while True:
        #Update the total number of used cpus
        #try: 
        #    while True:
        #        pCpu = cpuQueue.get_nowait()
        #        usedCpus -= pCpu
        #        assert usedCpus >= 0
        #        cpuQueue.task_done()
        #except Empty:
        #    pass
        
        #Get a job from the queue
        job = queue.get()
        followOnJob = job.find("followOns").findall("followOn")[-1]
        memory = int(followOnJob.attrib["memory"])
        cpu = int(followOnJob.attrib["cpu"])
        assert cpu < sys.maxint
        assert memory < sys.maxint
        jobFile = job.attrib["file"]
        jobCommand = "%s -E %s %s --job %s" % (sys.executable, jobTreeSlavePath, os.path.split(workflowRootPath())[0], jobFile)
        
        #Deal with the minimum cpus
        #if cpu > maxCpus:
        #    raise RuntimeError("A job is requesting more CPUs than available. Requested: %i, Available: %i" % (cpu, maxCpus))
        #usedCpus += cpu
        #while usedCpus > maxCpus:
        #    pCpu = cpuQueue.get()
        #    usedCpus -= pCpu
        #    assert usedCpus >= 0
        #    cpuQueue.task_done()
        
        #Now, finally, issue the job!
        lock.acquire()
        try:
            jobID = batchSystem.issueJob(jobCommand, memory, cpu, job.attrib["slave_log_file"])
            jobIDsToJobsHash[jobID] = (jobFile, cpu)
            logger.debug("Issued the job: %s with job id: %i and cpus: %i" % (jobFile, jobID, cpu))
        finally:
            lock.release()
        queue.task_done()
        
class JobBatcher:
    """Class works with jobBatcherWorker to submit jobs to the batch system, asynchronously
    of of the master class. 
    """
    def __init__(self, batchSystem, maxCpus):
        self.jobIDsToJobsHash = {}
        self.queue = Queue()
        self.lock = Lock()
        self.cpuQueue = Queue()
        #worker = Thread(target=jobBatcherWorker, args=(batchSystem, maxCpus, self.queue, self.lock, self.jobIDsToJobsHash, self.cpuQueue))
        #worker.setDaemon(True)
        #worker.start()
        self.jobsIssued = 0
        
    def issueJob(self, job):
        """Add a job to the queue of jobs
        """
        self.jobsIssued += 1
        #self.queue.put(job)
        followOnJob = job.find("followOns").findall("followOn")[-1]
        memory = int(followOnJob.attrib["memory"])
        cpu = int(followOnJob.attrib["cpu"])
        assert cpu < sys.maxint
        assert memory < sys.maxint
        jobFile = job.attrib["file"]
        jobCommand = "%s -E %s %s --job %s" % (sys.executable, jobTreeSlavePath, os.path.split(workflowRootPath())[0], jobFile)
        jobID = batchSystem.issueJob(jobCommand, memory, cpu, job.attrib["slave_log_file"])
        jobIDsToJobsHash[jobID] = (jobFile, cpu)
        logger.debug("Issued the job: %s with job id: %i and cpus: %i" % (jobFile, jobID, cpu))
    
    def issueJobs(self, jobs):
        """Add a list of jobs
        """
        for job in jobs:
            self.issueJob(job)
    
    def getNumberOfJobsIssued(self):
        """Gets number of jobs that have been added by issueJob(s) and not removed by removeJobID
        """
        assert self.jobsIssued >= 0
        return self.jobsIssued
    
    def getJob(self, jobID):
        """Gets the job file associated the a given id
        """
        self.lock.acquire()
        try:
            return self.jobIDsToJobsHash[jobID][0]
        finally:
            self.lock.release()
    
    def hasJob(self, jobID):
        """Returns true if the jobID is in the list of jobs.
        """
        self.lock.acquire()
        try:
            return self.jobIDsToJobsHash.has_key(jobID)
        finally:
            self.lock.release()
        
    def getJobIDs(self):
        """Gets the set of jobs currently issued.
        """
        self.lock.acquire()
        try:
            return self.jobIDsToJobsHash.keys()
        finally:
            self.lock.release()
    
    def removeJobID(self, jobID):
        """Removes a job from the jobBatcher.
        """
        self.lock.acquire()
        try:
            assert jobID in self.jobIDsToJobsHash
            self.jobsIssued -= 1
            jobFile, cpu = self.jobIDsToJobsHash.pop(jobID)
            #self.cpuQueue.put(cpu)
            return jobFile
        finally:
            self.lock.release()
    
def fixJobsList(config, jobFiles):
    """Traverses through and finds any .old files, using there saved state to recover a 
    valid state of the job tree.
    """
    updatingFiles = []
    for fileName in jobFiles[:]:
        if "updating" in fileName:
            updatingFiles.append(fileName)
    
    for updatingFileName in updatingFiles: #Things crashed while the state was updating, so we should remove the 'updating' and '.new' files
        fileHandle = open(updatingFileName, 'r')
        for fileName in fileHandle.readline().split():
            if os.path.isfile(fileName):
                config.attrib["job_file_dir"].destroyTempFile(fileName)
                jobFiles.remove(fileName)
        fileHandle.close()
        config.attrib["job_file_dir"].destroyTempFile(updatingFileName)
        jobFiles.remove(updatingFileName)
    
    for fileName in jobFiles[:]: #Else any '.new' files will be switched in place of the original file. 
        if fileName[-3:] == 'new':
            originalFileName = fileName[:-4]
            os.rename(fileName, originalFileName)
            jobFiles.remove(fileName)
            if originalFileName not in jobFiles:
                jobFiles.append(originalFileName)
            logger.critical("Fixing the file: %s from %s" % (originalFileName, fileName))

def restartFailedJobs(config, jobFiles):
    """Traverses through the file tree and resets the restart count of all jobs.
    """
    for absFileName in jobFiles:
        if os.path.isfile(absFileName):
            job = readJob(absFileName)
            logger.info("Restarting job: %s" % job.attrib["file"])
            job.attrib["remaining_retry_count"] = config.attrib["retry_count"]
            if job.attrib["colour"] == "red":
                job.attrib["colour"] = "grey"
            writeJobs([ job ])

def processFinishedJob(jobID, resultStatus, updatedJobFiles, jobBatcher):
    """Function reads a processed job file and updates it state.
    """
    jobFile = jobBatcher.removeJobID(jobID)
    
    updatingFileIsPresent = os.path.isfile(jobFile + ".updating")
    newFileIsPresent = os.path.isfile(jobFile + ".new")
    
    if resultStatus == 0 and updatingFileIsPresent:
        logger.critical("Despite the batch system claiming success there is a .updating file present: %s", jobFile + ".updating")
        
    if resultStatus == 0 and newFileIsPresent:
        logger.critical("Despite the batch system claiming success there is a .new file present: %s", jobFile + ".new")

    if resultStatus != 0 or newFileIsPresent or updatingFileIsPresent: #Job not successful according to batchsystem, or according to the existance of a .new or .updating file
        if updatingFileIsPresent: #The job failed while attempting to write the job file.
            logger.critical("There was an .updating file for the crashed job: %s" % jobFile)
            if os.path.isfile(jobFile + ".new"): #The job failed while writing the updated job file.
                logger.critical("There was an .new file for the crashed job: %s" % jobFile)
                os.remove(jobFile + ".new") #The existance of the .updating file means it wasn't complete
            os.remove(jobFile + ".updating") #Delete second the updating file second to preserve a correct state
            assert os.path.isfile(jobFile)
            job = readJob(jobFile) #The original must still be there.
            assert job.find("children").find("child") == None #The original can not reflect the end state of the job.
            assert int(job.attrib["black_child_count"]) == int(job.attrib["child_count"])
            job.attrib["colour"] = "red" #It failed, so we mark it so and continue.
            writeJobs([ job ])
            logger.critical("We've reverted to the original job file and marked it as failed: %s" % jobFile)
        else:
            if newFileIsPresent: #The job was not properly updated before crashing
                logger.critical("There is a valid .new file %s" % jobFile)
                if os.path.isfile(jobFile):
                    os.remove(jobFile)
                os.rename(jobFile + ".new", jobFile)
                job = readJob(jobFile)
                if job.attrib["colour"] == "grey": #The job failed while preparing to run another job on the slave
                    assert job.find("children").find("child") == None #File 
                    job.attrib["colour"] = "red"
                    writeJobs([ job ])
                assert job.attrib["colour"] in ("black", "red")
            else:
                logger.critical("There was no valid .new file %s" % jobFile)
                assert os.path.isfile(jobFile)
                job = readJob(jobFile) #The job may have failed before or after creating this file, we check the state.
                if job.attrib["colour"] == "black": #The job completed okay, so we'll keep it
                    logger.critical("Despite the batch system job failing, the job appears to have completed okay")
                else:
                    assert job.attrib["colour"] in ("grey", "red")
                    assert job.find("children").find("child") == None #File 
                    assert int(job.attrib["black_child_count"]) == int(job.attrib["child_count"])
                    if job.attrib["colour"] == "grey":
                        job.attrib["colour"] = "red"
                        writeJobs([ job ])
                    logger.critical("We've reverted to the original job file and marked it as failed: %s" % jobFile)

    assert jobFile not in updatedJobFiles
    updatedJobFiles.add(jobFile) #Now we know the job is done we can add it to the list of updated job files
    logger.debug("Added job: %s to active jobs" % jobFile)

def reissueOverLongJobs(updatedJobFiles, jobBatcher, config, batchSystem):
    """Check each issued job - if it is running for longer than desirable.. issue a kill instruction.
    Wait for the job to die then we pass the job to processFinishedJob.
    """
    maxJobDuration = float(config.attrib["max_job_duration"])
    idealJobTime = float(config.attrib["job_time"])
    if maxJobDuration < idealJobTime * 10:
        logger.info("The max job duration is less than 10 times the ideal the job time, so I'm setting it to the ideal job time, sorry, but I don't want to crash your jobs because of limitations in jobTree ")
        maxJobDuration = idealJobTime * 10
    if maxJobDuration < 10000000: #We won't both doing anything is the rescue time is more than 16 weeks.
        runningJobs = batchSystem.getRunningJobIDs()
        for jobID in runningJobs.keys():
            if runningJobs[jobID] > maxJobDuration:
                logger.critical("The job: %s has been running for: %s seconds, more than the max job duration: %s, we'll kill it" % \
                            (jobBatcher.getJob(jobID), str(runningJobs[jobID]), str(maxJobDuration)))
                batchSystem.killJobs([ jobID ])
                processFinishedJob(jobID, 1, updatedJobFiles, jobBatcher)

reissueMissingJobs_missingHash = {} #Hash to store number of observed misses
def reissueMissingJobs(updatedJobFiles, jobBatcher, batchSystem, killAfterNTimesMissing=3):
    """Check all the current job ids are in the list of currently running batch system jobs. 
    If a job is missing, we mark it as so, if it is missing for a number of runs of 
    this function (say 10).. then we try deleting the job (though its probably lost), we wait
    then we pass the job to processFinishedJob.
    """
    runningJobs = set(batchSystem.getIssuedJobIDs())
    jobIDsSet = set(jobBatcher.getJobIDs())
    #Clean up the reissueMissingJobs_missingHash hash
    missingJobIDsSet = set(reissueMissingJobs_missingHash.keys())
    for jobID in missingJobIDsSet.difference(jobIDsSet):
        reissueMissingJobs_missingHash.pop(jobID)
    assert runningJobs.issubset(jobIDsSet) #Assert checks we have no unexpected jobs running
    for jobID in set(jobIDsSet.difference(runningJobs)):
        jobFile = jobBatcher.getJob(jobID)
        if reissueMissingJobs_missingHash.has_key(jobID):
            reissueMissingJobs_missingHash[jobID] = reissueMissingJobs_missingHash[jobID]+1
        else:
            reissueMissingJobs_missingHash[jobID] = 1
        timesMissing = reissueMissingJobs_missingHash[jobID]
        logger.critical("Job %s with id %i is missing for the %i time" % (jobFile, jobID, timesMissing))
        if timesMissing == killAfterNTimesMissing:
            reissueMissingJobs_missingHash.pop(jobID)
            batchSystem.killJobs([ jobID ])
            processFinishedJob(jobID, 1, updatedJobFiles, jobBatcher)
    return len(reissueMissingJobs_missingHash) == 0 #We use this to inform if there are missing jobs

def reportJobLogFiles(job):
    logger.critical("The log file of the job")
    logFile(job.attrib["log_file"], logger.critical)
    logger.critical("The log file of the slave for the job")
    logFile(job.attrib["slave_log_file"], logger.critical) #We log the job log file in the main loop
    
def mainLoop(config, batchSystem):
    """This is the main loop from which jobs are issued and processed.
    """
    rescueJobsFrequency = float(config.attrib["rescue_jobs_frequency"])
    maxJobDuration = float(config.attrib["max_job_duration"])
    assert maxJobDuration >= 0
    logger.info("Got parameters,rescue jobs frequency: %s max job duration: %s" % \
                (rescueJobsFrequency, maxJobDuration))
    
    #Kill any jobs on the batch system queue from the last time.
    assert len(batchSystem.getIssuedJobIDs()) == 0 #Batch system must start with no active jobs!
    logger.info("Checked batch system has no running jobs and no updated jobs")
    
    jobFiles = config.attrib["job_file_dir"].listFiles()
    logger.info("Got a list of job files")
    
    #Repair the job tree using any .old files
    fixJobsList(config, jobFiles)
    logger.info("Fixed the job files using any .old files")
    
    #Get jobs that were running, or that had failed reset to 'grey' status
    restartFailedJobs(config, jobFiles)
    logger.info("Reworked failed jobs")
    
    updatedJobFiles = set() #Jobs whose status needs updating, either because they have finished, or because they need to be started.
    for jobFile in jobFiles:
        job = readJob(jobFile)
        if job.attrib["colour"] not in ("blue"):
            updatedJobFiles.add(jobFile)
    logger.info("Got the active (non blue) job files")
    
    totalJobFiles = len(jobFiles) #Total number of job files we have.
    jobBatcher = JobBatcher(batchSystem, int(config.attrib["max_jobs"]))
    
    idealJobTime = float(config.attrib["job_time"]) 
    assert idealJobTime > 0.0
    
    reportAllJobLogFiles = bool(int(config.attrib["reportAllJobLogFiles"]))
    
    stats = config.attrib.has_key("stats")
    if stats:
        startTime = time.time()
        startClock = getTotalCpuTime()
        
    logger.info("Starting the main loop")
    timeSinceJobsLastRescued = time.time() - rescueJobsFrequency + 100 #We hack it so that we rescue jobs after the first 100 seconds to get around an apparent parasol bug

    while True: 
        if len(updatedJobFiles) > 0:
            logger.debug("Built the jobs list, currently have %i job files, %i jobs to update and %i jobs currently issued" % (totalJobFiles, len(updatedJobFiles), jobBatcher.getNumberOfJobsIssued()))
        
        for jobFile in list(updatedJobFiles):
            job = readJob(jobFile)
            assert job.attrib["colour"] is not "blue"
            
            ##Check the log files exist, because they must ultimately be cleaned up by their respective file trees.
            def checkFileExists(fileName, type):
                if not os.path.isfile(fileName): #We need to keep these files in existence.
                    open(fileName, 'w').close()
                    logger.critical("The file %s of type %s for job %s had disappeared" % (fileName, type, jobFile))
            checkFileExists(job.attrib["log_file"], "log_file")
            checkFileExists(job.attrib["slave_log_file"], "slave_log_file")
            if stats:
                checkFileExists(job.attrib["stats"], "stats")
            
            def reissueJob(job):
                #Reset the log files for the job.
                updatedJobFiles.remove(jobFile)
                open(job.attrib["slave_log_file"], 'w').close()
                open(job.attrib["log_file"], 'w').close()
                assert job.attrib["colour"] == "grey"
                jobBatcher.issueJob(job)
                
            def makeGreyAndReissueJob(job):
                job.attrib["colour"] = "grey"
                writeJobs([ job ])
                reissueJob(job)
            
            if job.attrib["colour"] == "grey": #Get ready to start the job
                reissueJob(job)
            elif job.attrib["colour"] == "black": #Job has finished okay
                logger.debug("Job: %s has finished okay" % job.attrib["file"])
                if reportAllJobLogFiles:
                    reportJobLogFiles(job)
                #Deal with stats
                if stats:
                    system("cat %s >> %s" % (job.attrib["stats"], config.attrib["stats"]))
                    open(job.attrib["stats"], 'w').close() #Reset the stats file
                if job.find("messages") != None:
                    for message in job.find("messages").findall("message"):
                        logger.critical("Received the following message from job: %s" % message.attrib["message"])
                    job.remove(job.find("messages"))
                childCount = int(job.attrib["child_count"])
                blackChildCount = int(job.attrib["black_child_count"])
                assert childCount == blackChildCount #Has no currently running child jobs
                #Launch any unborn children
                unbornChildren = job.find("children").findall("child")
                if len(unbornChildren) > 0: #unbornChild != None: #We must give birth to the unborn children
                    logger.debug("Job: %s has %i children to schedule" % (job.attrib["file"], len(unbornChildren)))
                    newChildren = []
                    for unbornChild in unbornChildren:
                        newJob = createJob(unbornChild.attrib, job.attrib["file"], config)
                        totalJobFiles += 1
                        newChildren.append(newJob)
                    job.find("children").clear() #removeall("child")
                    updatedJobFiles.remove(job.attrib["file"])
                    job.attrib["child_count"] = str(childCount + len(newChildren))
                    job.attrib["colour"] = "blue" #Blue - has children running.
                    writeJobs([ job ] + newChildren ) #Check point
                    jobBatcher.issueJobs(newChildren)
                    
                elif len(job.find("followOns").findall("followOn")) != 0: #Has another job
                    logger.debug("Job: %s has a new command that we can now issue" % job.attrib["file"])
                    ##Reset the job run info
                    job.attrib["remaining_retry_count"] = config.attrib["retry_count"]
                    makeGreyAndReissueJob(job)
                    
                else: #Job has finished, so we can defer to any parent
                    logger.debug("Job: %s is now dead" % job.attrib["file"])
                    job.attrib["colour"] = "dead"
                    if job.attrib.has_key("parent"):
                        parent = readJob(job.attrib["parent"])
                        assert job.attrib["parent"] != jobFile
                        assert parent.attrib["colour"] == "blue"
                        assert int(parent.attrib["black_child_count"]) < int(parent.attrib["child_count"])
                        parent.attrib["black_child_count"] = str(int(parent.attrib["black_child_count"]) + 1)
                        if int(parent.attrib["child_count"]) == int(parent.attrib["black_child_count"]):
                            parent.attrib["colour"] = "black"
                            assert parent.attrib["file"] not in updatedJobFiles
                            updatedJobFiles.add(parent.attrib["file"])
                        writeJobs([ job, parent ]) #Check point
                    updatedJobFiles.remove(job.attrib["file"])
                    totalJobFiles -= 1
                    deleteJob(job, config)
                         
            elif job.attrib["colour"] == "red": #Job failed
                logger.critical("Job: %s failed" % job.attrib["file"])
                reportJobLogFiles(job)
                #Checks
                assert len(job.find("children").findall("child")) == 0
                assert int(job.attrib["child_count"]) == int(job.attrib["black_child_count"])
                
                remainingRetryCount = int(job.attrib["remaining_retry_count"])
                if remainingRetryCount > 0: #Give it another try, maybe there is a bad node somewhere
                    job.attrib["remaining_retry_count"] = str(remainingRetryCount-1)
                    logger.critical("Job: %s will be restarted, it has %s goes left" % (job.attrib["file"], job.attrib["remaining_retry_count"]))
                    makeGreyAndReissueJob(job)
                else:
                    assert remainingRetryCount == 0
                    updatedJobFiles.remove(job.attrib["file"]) #We remove the job and neither delete it or reissue it
                    logger.critical("Job: %s is completely failed" % job.attrib["file"])
                    
            else: #This case should only occur after failure
                logger.debug("Job: %s is already dead, we'll get rid of it" % job.attrib["file"])
                assert job.attrib["colour"] == "dead"
                updatedJobFiles.remove(job.attrib["file"])
                totalJobFiles -= 1
                deleteJob(job, config)
        
        if len(updatedJobFiles) == 0:
            if jobBatcher.getNumberOfJobsIssued() == 0:
                logger.info("Only failed jobs and their dependents (%i total) are remaining, so exiting." % totalJobFiles)
                break 
            updatedJob = batchSystem.getUpdatedJob(10) #Asks the batch system what jobs have been completed.
            if updatedJob != None: #Runs through a map of updated jobs and there status, 
                jobID, result = updatedJob
                if jobBatcher.hasJob(jobID): 
                    if result == 0:
                        logger.debug("Batch system is reporting that the job %s ended successfully" % jobBatcher.getJob(jobID))   
                    else:
                        logger.critical("Batch system is reporting that the job %s failed with exit value %i" % (jobBatcher.getJob(jobID), result))  
                    processFinishedJob(jobID, result, updatedJobFiles, jobBatcher)
                else:
                    logger.info("A result seems to already have been processed: %i" % jobID)
        
        if time.time() - timeSinceJobsLastRescued >= rescueJobsFrequency: #We only rescue jobs every N seconds
            reissueOverLongJobs(updatedJobFiles, jobBatcher, config, batchSystem)
            logger.info("Reissued any over long jobs")
            
            hasNoMissingJobs = reissueMissingJobs(updatedJobFiles, jobBatcher, batchSystem)
            if hasNoMissingJobs:
                timeSinceJobsLastRescued = time.time()
            else:
                timeSinceJobsLastRescued += 60 #This means we'll try again in 60 seconds
            logger.info("Rescued any (long) missing jobs")
    
    if stats:
        fileHandle = open(config.attrib["stats"], 'a')
        fileHandle.write("<total_time time='%s' clock='%s'/></stats>" % (str(time.time() - startTime), str(getTotalCpuTime() - startClock)))
        fileHandle.close()
    
    logger.info("Finished the main loop")     
    
    return totalJobFiles #Returns number of failed jobs
