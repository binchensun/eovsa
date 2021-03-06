#
# Pipeline Calibration Routines
#
# These routines are used to apply calibration to the output of the
# udb_util.py's readXdata() routine.
#
# History
#  2017-08-06  DG
#    Initial start of file
#  2017-08-07  DG
#    Further additions and debugging
#
import dbutil as db
import numpy as np
from util import Time, nearest_val_idx, common_val_idx, lobe
import stateframe
import cal_header as ch

def get_sql_info(trange):
   ''' Get all antenna information from the SQL database for a given 
       timerange, including TrackFlag and Parallactic Angle
   '''
   cursor = db.get_cursor()
   sqldict = db.get_dbrecs(cursor,dimension=15,timestamp=trange)
   azeldict = stateframe.azel_from_sqldict(sqldict)
   time = Time(sqldict['Timestamp'][:,0].astype(int),format='lv')
   azeldict.update({'Time':time})
   cursor.close()
   return azeldict

def get_bl_order():
    """Return the order of baseline data output by a CASPER correlator
    X engine."""
    n_ants = 16
    order1, order2 = [], []
    for i in range(n_ants):
        for j in range(int(n_ants/2),-1,-1):
            k = (i-j) % n_ants
            if i >= k: order1.append((k, i))
            else: order2.append((i, k))
    order2 = [o for o in order2 if o not in order1]
    return tuple([o for o in order1 + order2])

def apply_attn_corr(data, tref=None):
    ''' Applys the attenuator state corrections to the given data dictionary,
        corrected to the gain-state at time given by Time() object tref.
        
        Inputs:
          data     A dictionary returned by udb_util.py's readXdata().
          tref     A Time() object with the reference time, or if None,
                     the gain state of the nearest earlier REFCAL is 
                     used.
        Output:
          cdata    A dictionary with the gain-corrected data.  The keys
                     px, py, and x, are updated.
                     
        NB: This is the same routine as in gaincal2.py, but modified
        to handle the different ordering/format of data from udb_util.py's
        readXdata() routine.
    '''
    from gaincal2 import get_gain_state
    from util import common_val_idx, nearest_val_idx
    import copy
    if tref is None:
        # No reference time specified, so get nearest earlier REFCAL
        trange = Time(data['time'][[0,-1]],format='jd')
        xml, buf = ch.read_cal(8,t=trange[0])
        tref = Time(stateframe.extract(buf,xml['Timestamp']),format='lv')
    # Get the gain state at the reference time (actually median over 1 minute)
    trefrange = Time([tref.iso,Time(tref.lv+60,format='lv').iso])
    ref_gs =  get_gain_state(trefrange)  # refcal gain state for 60 s
    # Get median of refcal gain state (which should be constant anyway)
    ref_gs['h1'] = np.median(ref_gs['h1'],1)
    ref_gs['h2'] = np.median(ref_gs['h2'],1)
    ref_gs['v1'] = np.median(ref_gs['v1'],1)
    ref_gs['v2'] = np.median(ref_gs['v2'],1)

    # Get timerange from data
    trange = Time([data['time'][0],data['time'][-1]],format='jd')
    # Get time cadence
    dt = np.int(np.round(np.median(data['time'][1:] - data['time'][:-1]) * 86400))
    if dt == 1: dt = None
    # Get the gain state of the requested timerange
    src_gs = get_gain_state(trange,dt)   # solar gain state for timerange of file
    nt = len(src_gs['times'])
    antgain = np.zeros((15,2,34,nt),np.float32)   # Antenna-based gains vs. band
    for i in range(15):
        for j in range(34):
            antgain[i,0,j] = src_gs['h1'][i] + src_gs['h2'][i] - ref_gs['h1'][i] - ref_gs['h2'][i] + src_gs['dcmattn'][i,0,j] - ref_gs['dcmattn'][i,0,j]
            antgain[i,1,j] = src_gs['v1'][i] + src_gs['v2'][i] - ref_gs['v1'][i] - ref_gs['v2'][i] + src_gs['dcmattn'][i,1,j] - ref_gs['dcmattn'][i,1,j]

    cdata = copy.deepcopy(data)
    # Create giant array of baseline-based gains, translated to baselines and frequencies
    fghz = data['fghz']
    nf = len(fghz)
    blist = (fghz*2 - 1).astype(int) - 1       # Band list corresponding to frequencies in data
    blgain = np.zeros((nf,136,4,nt),float)     # Baseline-based gains vs. frequency
    for k,bl in enumerate(get_bl_order()):
        i, j = bl
        if i < 15 and j < 15:
            blgain[:,k,0] = 10**((antgain[i,0,blist] + antgain[j,0,blist])/20.)
            blgain[:,k,1] = 10**((antgain[i,1,blist] + antgain[j,1,blist])/20.)
            blgain[:,k,2] = 10**((antgain[i,0,blist] + antgain[j,1,blist])/20.)
            blgain[:,k,3] = 10**((antgain[i,1,blist] + antgain[j,0,blist])/20.)
    # Reorder antgain axes to put frequencies in first slot, to match data
    antgain = np.swapaxes(np.swapaxes(antgain,1,2),0,1)
    antgainf = 10**(antgain[blist]/10.)

    idx = nearest_val_idx(data['time'],src_gs['times'].jd)
    # Correct the auto- and cross-correlation data
    cdata['x'] *= blgain[:,:,:,idx]
    # Reshape px and py arrays
    cdata['px'].shape = (134,16,3,nt)
    cdata['py'].shape = (134,16,3,nt)
    # Correct the power
    cdata['px'][:,:15,0] *= antgainf[:,:,0,idx]
    cdata['py'][:,:15,0] *= antgainf[:,:,1,idx]
    # Correct the power-squared
    cdata['px'][:,:15,1] *= antgainf[:,:,0,idx]**2
    cdata['py'][:,:15,1] *= antgainf[:,:,1,idx]**2
    # Reshape px and py arrays back to original
    cdata['px'].shape = (134*16*3,nt)
    cdata['py'].shape = (134*16*3,nt)
    return cdata
    
def get_calfac(t=None):
    ''' Read total power and auto-correlation calibration factors from the SQL
        database, for the time specified by Time() object t, or if None, at the
        next earlier calibration time to the current time.
    '''
    tpcal_type = 10  # Calibration type specified in cal_header.py
    if t is None:
        t = Time.now()
    xml, buf = ch.read_cal(tpcal_type,t=t)
    fghz = stateframe.extract(buf,xml['FGHz'])
    nf = len(fghz)
    tpcalfac = np.zeros((13,2,nf),np.float)
    tpoffsun = np.zeros((13,2,nf),np.float)
    accalfac = np.zeros((13,2,nf),np.float)
    acoffsun = np.zeros((13,2,nf),np.float)
    nant = len(xml['Antenna'])
    for i in range(nant):
        iant = stateframe.extract(buf,xml['Antenna'][i]['Antnum'])-1
        tpcalfac[iant] = stateframe.extract(buf,xml['Antenna'][i]['TPCalfac'])
        accalfac[iant] = stateframe.extract(buf,xml['Antenna'][i]['ACCalfac'])
        tpoffsun[iant] = stateframe.extract(buf,xml['Antenna'][i]['TPOffsun'])
        acoffsun[iant] = stateframe.extract(buf,xml['Antenna'][i]['ACOffsun'])
    return {'fghz':fghz,'timestamp':stateframe.extract(buf,xml['Timestamp']),
            'tpcalfac':tpcalfac,'accalfac':accalfac,'tpoffsun':tpoffsun,'acoffsun':acoffsun}
            
def unrot(data, azeldict=None):
    ''' Apply the correction to differential feed rotation to data, and return
        the corrected data.

        Inputs:
          data     A dictionary returned by udb_util.py's readXdata().
          azeldict The dictionary returned from get_sql_info(), or if None, the appropriate
                     get_sql_info() call is done internally.

        Output:
          cdata    A dictionary with the phase-corrected data.  Only the key
                     x is updated.
    '''
    import copy
    from util import lobe
    trange = Time(data['time'][[0,-1]],format='jd')

    if azeldict is None:
        azeldict = get_sql_info(trange)
    chi = azeldict['ParallacticAngle']  # (nt, nant)
    # Correct parallactic angle for equatorial mounts, relative to Ant14
    for i in [8,9,10,12,13]:
        chi[:,i] -= chi[:,13]
        
    # Ensure that nearest valid parallactic angle is used for times in the data
    good, = np.where(azeldict['ActualAzimuth'][0] != 0)
    tidx = nearest_val_idx(data['time'],azeldict['Time'][good].jd)

    # Read X-Y Delay phase from SQL database and get common frequencies
    xml, buf = ch.read_cal(11,t=trange[0])
    fghz = stateframe.extract(buf,xml['FGHz'])
    good, = np.where(fghz != 0.)
    fghz = fghz[good]
    dph = stateframe.extract(buf,xml['XYphase'])
    dph = dph[:,good]
    fidx1, fidx2 = common_val_idx(data['fghz'],fghz,precision=4)
    missing = np.setdiff1d(np.arange(len(data['fghz'])),fidx1)
    
    nf, nbl, npol, nt = data['x'].shape
    nf = len(fidx1)
    # Correct data for X-Y delay phase
    for k,bl in enumerate(get_bl_order()):
        i, j = bl
        if i < 14 and j < 14 and i != j:
            a1 = lobe(dph[i,fidx2] - dph[j,fidx2])
            a2 = -dph[j,fidx2] + np.pi/2
            a3 = dph[i,fidx2] - np.pi/2
            data['x'][fidx1,k,1] *= np.repeat(np.exp(1j*a1),nt).reshape(nf,nt)
            data['x'][fidx1,k,2] *= np.repeat(np.exp(1j*a2),nt).reshape(nf,nt) 
            data['x'][fidx1,k,3] *= np.repeat(np.exp(1j*a3),nt).reshape(nf,nt)
    
    # Correct data for differential feed rotation
    cdata = copy.deepcopy(data)
    for n in range(nt):
        for k,bl in enumerate(get_bl_order()):
            i, j = bl
            if i < 14 and j < 14 and i != j:
                dchi = chi[n,i] - chi[n,j]
                cchi = np.cos(dchi)
                schi = np.sin(dchi)
                cdata['x'][:,k,0,n] = data['x'][:,k,0,n]*cchi + data['x'][:,k,3,n]*schi
                cdata['x'][:,k,2,n] = data['x'][:,k,2,n]*cchi + data['x'][:,k,1,n]*schi
                cdata['x'][:,k,3,n] = data['x'][:,k,3,n]*cchi - data['x'][:,k,0,n]*schi
                cdata['x'][:,k,1,n] = data['x'][:,k,1,n]*cchi - data['x'][:,k,2,n]*schi
    # Set flags for any missing frequencies (hopefully this also works when missing is np.array([]))
    cdata[missing] = np.ma.masked
    return cdata