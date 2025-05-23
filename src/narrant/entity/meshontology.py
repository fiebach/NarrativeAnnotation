import json
import logging
from datetime import datetime

from kgextractiontoolbox.backend.database import Session
from kgextractiontoolbox.backend.models import EntityResolverData
from kgextractiontoolbox.progress import print_progress_with_eta
from narrant.config import MESH_DESCRIPTORS_FILE
from narrant.entitylinking.enttypes import DOSAGE_FORM, METHOD, DISEASE, VACCINE, HEALTH_STATUS, TISSUE, LAB_METHOD
from narrant.mesh.data import MeSHDB

MESH_TREE_NAMES = dict(
    A="Anatomy",
    B="Organisms",
    C="Diseases",
    D="Chemicals and Drugs",
    E="Analytical, Diagnostic and Therapeutic Techniques, and Equipment",
    F="Psychiatry and Psychology",
    G="Phenomena and Processes",
    H="Disciplines and Occupations",
    I="Anthropology, Education, Sociology, and Social Phenomena",
    J="Technology, Industry, and Agriculture",
    K="Humanities",
    L="Information Science",
    M="Named Groups",
    N="Health Care",
    V="Publication Characteristics",
    Z="Geographicals"
)

MESH_TREE_TO_ENTITY_TYPE = [
    ("D20.215.894", VACCINE),  # Vaccines
    ("D26.255", DOSAGE_FORM),  # Dosage Forms
    ("E02.319.300", DOSAGE_FORM),  # Drug Delivery Systems
    ("E02.319.267", DOSAGE_FORM),  # Drug Administration Routes
    ("J01.637.512.600", DOSAGE_FORM),  # Nanoparticles
    ("J01.637.512.850", DOSAGE_FORM),  # Nanotubes
    ("J01.637.512.925", DOSAGE_FORM),  # Nanowires
    ("E", METHOD),
    ("E", LAB_METHOD),  # Each method could be specified to a LabMethod. We don't know it here.
    ("E", DOSAGE_FORM),
    ("C", DISEASE),
    ("F03", DISEASE),
    ("F02", DISEASE),
    ("M01", HEALTH_STATUS),
    ("A10", TISSUE)
]


class MeSHOntology:
    """
    class to store the mesh ontology in a efficient tree structure
    """

    NAME = "MeSHOntology"

    __instance = None

    def __new__(cls):
        if cls.__instance is None:
            cls.__instance = super().__new__(cls)
        return cls.__instance

    def __init__(self):
        self.treeno2desc = {}
        self.descriptor2treeno = {}
        self.load_index()

    def _clear_index(self):
        """
        Clear the index by removing all entries from dictionaries
        :return: Nothing
        """
        self.treeno2desc = {}
        self.descriptor2treeno = {}

    def _add_descriptor_for_tree_no(self, descriptor_id, descriptor_heading, tree_no: str):
        """
        Stores the tree number as an index for the descriptor
        :param descriptor_id: MeSH Descriptor id
        :param descriptor_heading: MeSH Descriptor heading
        :param tree_no: Tree number as a String (e.g. C01.622....
        :return: Nothing
        """
        if tree_no in self.treeno2desc:
            raise KeyError('tree number is already mapped to: {}'.format(self.treeno2desc[tree_no]))
        self.treeno2desc[tree_no] = (descriptor_id, descriptor_heading)

    def find_descriptors_start_with_tree_no(self, tree_no: str) -> [(str, str)]:
        """
        Finds all descriptors which are in a tree starting with the tree number
        :param tree_no: tree number which should be the start of the descriptors
        :return: a list of descriptors (id, heading)
        """
        results = []
        visited = set()
        for d_tree_no, (d_id, d_heading) in self.treeno2desc.items():
            if d_id not in visited:
                if d_tree_no.startswith(tree_no):
                    results.append((d_id, d_heading))
                    visited.add(d_id)
        return results

    def get_descriptor_for_tree_no(self, tree_no: str) -> (str, str):
        """
        Gets a MeSH Descriptor for a tree number
        :param tree_no: MeSH tree number
        :return: (MeSH Descriptor id, MeSH Descriptor heading)
        """
        return self.treeno2desc[tree_no]

    def _add_tree_number_for_descriptor(self, descriptor_id: str, tree_no: str):
        """
        Add a tree number for a descriptor
        :param descriptor_id: MeSH descriptor id
        :param tree_no: Tree number for this descriptor
        :return:
        """
        if descriptor_id in self.descriptor2treeno:
            self.descriptor2treeno[descriptor_id].append(tree_no)
        else:
            self.descriptor2treeno[descriptor_id] = [tree_no]

    def get_tree_numbers_for_descriptor(self, descriptor_id) -> [str]:
        """
        Returns a list of tree numbers for a descriptor id
        :param descriptor_id: MeSH descriptor id
        :return: List of tree numbers
        """
        return self.descriptor2treeno[descriptor_id]

    def get_tree_numbers_with_entity_type_for_descriptor(self, descriptor_id: str) -> [str]:
        """
        Gets all tree numbers that are mapped to a known entity type
        raises key error if descriptor is not known
        :param descriptor_id: the mesh descriptor id
        :return: list of tree numbers
        """
        relevant_tree_numbers = []
        for tn in self.descriptor2treeno[descriptor_id]:
            try:
                if MeSHOntology.tree_number_to_entity_type(tn):
                    relevant_tree_numbers.append(tn)
            except KeyError:
                pass
        if len(relevant_tree_numbers) == 0:
            raise KeyError(f'Descriptor {descriptor_id} has no relevant tree numbers')
        return relevant_tree_numbers

    def get_entity_types_for_descriptor(self, descriptor_id: str) -> [str]:
        """
        Return all entity types for the descriptor id
        :param descriptor_id: a MeSH descriptor id
        :return: entity types
        """
        tree_nos = self.get_tree_numbers_for_descriptor(descriptor_id)
        ent_types = set()
        for tn in tree_nos:
            try:
                for et in MeSHOntology.tree_number_to_entity_type(tn):
                    ent_types.add(et)
            except KeyError:
                pass

        if len(ent_types) == 0:
            raise KeyError(f'Cannot decode entity type from MeSH {descriptor_id} (tree no.: {tree_nos})')

        return list(ent_types)

    @staticmethod
    def tree_number_to_entity_type(tree_number: str) -> [str]:
        """
        Computes the entity type for a given tree number
        raises a key error if no entity type was found
        :param tree_number: the tree number to check
        :return: a list of entity types
        """
        hits = []
        for tn, et in MESH_TREE_TO_ENTITY_TYPE:
            if tree_number.startswith(tn):
                hits.append(et)
        if hits:
            return hits
        else:
            raise KeyError(f'No entity type for tree number {tree_number} found')

    def __build_index_from_mesh(self, mesh_file=MESH_DESCRIPTORS_FILE):
        """
        Builds the index from a raw MeSH XML file
        :param mesh_file: Path to a MeSH XML file (default is the default MeSH descriptor path in the project config)
        :return: Nothing
        """
        self._clear_index()
        logging.info('Loading MeSH...')
        mesh = MeSHDB()
        mesh.load_xml(mesh_file)
        descs = mesh.get_all_descs()
        logging.info('Processing descriptors...')
        start_time = datetime.now()
        descriptor_count = len(descs)
        for idx, desc in enumerate(descs):
            for tn in desc.tree_numbers:
                self._add_descriptor_for_tree_no(desc.unique_id, desc.heading, tn)
                self._add_tree_number_for_descriptor(desc.unique_id, tn)
            print_progress_with_eta("building mesh ontology", idx, descriptor_count, start_time, print_every_k=1)
        logging.info('MeSH Ontology complete')

    def create_and_store_index(self):
        """
        Stores the whole MeSH ontology into database
        :return: Nothing
        """
        self.__build_index_from_mesh(mesh_file=MESH_DESCRIPTORS_FILE)

        logging.info('Storing index to database... ')
        session = Session.get()
        json_data = json.dumps(dict(treeno2desc=self.treeno2desc, descriptor2treeno=self.descriptor2treeno))
        EntityResolverData.overwrite_resolver_data(session, name=MeSHOntology.NAME, json_data=json_data)

    def load_index(self):
        """
        Loads the whole ontology from database
        :return: None
        """
        session = Session.get()
        json_data = EntityResolverData.load_data_from_json(session, name=MeSHOntology.NAME)
        if "treeno2desc" in json_data and "descriptor2treeno" in json_data:
            self.treeno2desc = json_data["treeno2desc"]
            self.descriptor2treeno = json_data["descriptor2treeno"]
        else:
            self.treeno2desc = {}
            self.descriptor2treeno = {}

    def retrieve_subdescriptors(self, decriptor_id: str) -> [(str)]:
        """
        retrieves a list of all sub-descriptors for a given descriptor
        :param decriptor_id: a mesh descriptor id
        :return: a list of sub-descriptor (id, heading)
        """
        tree_nos = self.get_tree_numbers_for_descriptor(descriptor_id=decriptor_id)
        sub_descriptors = set()
        for t_n in tree_nos:
            for res in self.find_descriptors_start_with_tree_no(t_n):
                sub_descriptors.add(res)
        return sub_descriptors

    def retrieve_superdescriptors(self, decriptor_id: str) -> [(str)]:
        """
        retrieves a list of all super-descriptors for a given descriptor
        :param decriptor_id: a mesh descriptor id
        :return: a list of super-descriptor (id, heading)
        """
        tree_nos = self.get_tree_numbers_for_descriptor(descriptor_id=decriptor_id)
        super_descriptors = set()
        for tn in tree_nos:
            # Numbers are organized as follows:
            # C18.452.394.750
            # C19.246
            # So we need to split by each '.'
            # Iterate over tn as long as '.' between it (results in 'C18.452.394', 'C18.452', 'C18')
            while '.' in tn:
                tn = tn.rpartition('.')[0]
                super_descriptors.add(self.treeno2desc[tn])

        return super_descriptors

    @staticmethod
    def get_name_for_tree(tree_start_character):
        """
        Returns the official name for the mesh tree name
        :param tree_start_character: the starting character
        :return: the official mesh tree name
        """
        return MESH_TREE_NAMES[tree_start_character]


def main():
    logging.basicConfig(format='%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
                        datefmt='%Y-%m-%d:%H:%M:%S',
                        level=logging.DEBUG)

    logging.info('Computing entity ontology index...')
    entity_ontology = MeSHOntology()
    entity_ontology.create_and_store_index()
    logging.info('Finished')


if __name__ == "__main__":
    main()
